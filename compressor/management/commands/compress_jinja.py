# flake8: noqa
import sys
from compressor.contrib.jinja2ext import CompressorExtension
import jinja2
import jinja2.meta
from jinja2 import Template
from jinja2.nodes import CallBlock, Call, ExtensionAttribute, If, Extends
from jinja2.exceptions import TemplateSyntaxError
from types import MethodType
from optparse import make_option

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO  # noqa

from django.core.management.base import NoArgsCommand, CommandError
from django.template import (TemplateDoesNotExist)
from django.utils.datastructures import SortedDict

try:
    from django.template.loaders.cached import Loader as CachedLoader
except ImportError:
    CachedLoader = None  # noqa

from compressor.cache import get_offline_hexdigest, write_offline_manifest
from compressor.conf import settings
from compressor.exceptions import OfflineGenerationError
from compressor.templatetags.compress import CompressorNode

class Command(NoArgsCommand):
    help = "Compress content outside of the request/response cycle"
    option_list = NoArgsCommand.option_list + (
        make_option('--extension', '-e', action='append', dest='extensions',
                    help='The file extension(s) to examine (default: ".html", '
                    'separate multiple extensions with commas, or use -e '
                    'multiple times)'),
        make_option('-f', '--force', default=False, action='store_true',
                    help="Force the generation of compressed content even if the "
                    "COMPRESS_ENABLED setting is not True.", dest='force'),
        make_option('--follow-links', default=False, action='store_true',
                    help="Follow symlinks when traversing the COMPRESS_ROOT "
                    "(which defaults to MEDIA_ROOT). Be aware that using this "
                    "can lead to infinite recursion if a link points to a parent "
                    "directory of itself.", dest='follow_links'),
    )

    requires_model_validation = False

    def get_loaders(self):
        from django.template.loader import template_source_loaders
        if template_source_loaders is None:
            try:
                from django.template.loader import (
                    find_template as finder_func)
            except ImportError:
                from django.template.loader import (
                    find_template_source as finder_func)  # noqa
            try:
                # Force django to calculate template_source_loaders from
                # TEMPLATE_LOADERS settings, by asking to find a dummy template
                source, name = finder_func('test')
            except TemplateDoesNotExist:
                pass
            # Reload template_source_loaders now that it has been calculated ;
            # it should contain the list of valid, instanciated template loaders
            # to use.
            from django.template.loader import template_source_loaders
        loaders = []
        # If template loader is CachedTemplateLoader, return the loaders
        # that it wraps around. So if we have
        # TEMPLATE_LOADERS = (
        #    ('django.template.loaders.cached.Loader', (
        #        'django.template.loaders.filesystem.Loader',
        #        'django.template.loaders.app_directories.Loader',
        #    )),
        # )
        # The loaders will return django.template.loaders.filesystem.Loader
        # and django.template.loaders.app_directories.Loader
        for loader in template_source_loaders:
            if CachedLoader is not None and isinstance(loader, CachedLoader):
                loaders.extend(loader.loaders)
            else:
                loaders.append(loader)
        return loaders

    def compress(self, log=None, **options):
        """
        Searches templates containing 'compress' nodes and compresses them
        "offline" -- outside of the request/response cycle.

        The result is cached with a cache-key derived from the content of the
        compress nodes (not the content of the possibly linked files!).
        """
        extensions = options.get('extensions')
        extensions = self.handle_extensions(extensions or ['html'])
        verbosity = int(options.get("verbosity", 0))
        if not log:
            log = StringIO()
        if not settings.TEMPLATE_LOADERS:
            raise OfflineGenerationError("No template loaders defined. You "
                                         "must set TEMPLATE_LOADERS in your "
                                         "settings.")
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(settings.TEMPLATE_DIRS),
                                 extensions=[CompressorExtension])
        templates = env.list_templates()

        if not templates:
            raise OfflineGenerationError("No templates found. Make sure your "
                                         "TEMPLATE_LOADERS and TEMPLATE_DIRS "
                                         "settings are correct.")
        if verbosity > 1:
            log.write("Found templates:\n\t" + "\n\t".join(templates) + "\n")

        compressor_nodes = SortedDict()
        for template_name in templates:
            try:
                template_content = env.loader.get_source(env, template_name)[0]
                template = env.parse(template_content)
            except IOError:  # unreadable file -> ignore
                if verbosity > 0:
                    log.write("Unreadable template at: %s\n" % template_name)
                continue
            except TemplateSyntaxError, e:  # broken template -> ignore
                if verbosity > 0:
                    log.write("Invalid template %s: %s\n" % (template_name, e))
                continue
            except TemplateDoesNotExist:  # non existent template -> ignore
                if verbosity > 0:
                    log.write("Non-existent template at: %s\n" % template_name)
                continue
            except UnicodeDecodeError:
                if verbosity > 0:
                    log.write("UnicodeDecodeError while trying to read "
                              "template %s\n" % template_name)
            nodes = list(self.walk_nodes(template))
            if nodes:
                template.template_name = template_name
                compressor_nodes.setdefault(template, []).extend(nodes)

        if not compressor_nodes:
            raise OfflineGenerationError(
                "No 'compress' template tags found in templates."
                "Try running compress command with --follow-links and/or"
                "--extension=EXTENSIONS")

        if verbosity > 0:
            log.write("Found 'compress' tags in:\n\t" +
                      "\n\t".join((t.template_name
                                   for t in compressor_nodes.keys())) + "\n")

        log.write("Compressing... ")
        count = 0
        results = []
        offline_manifest = SortedDict()
        for template, nodes in compressor_nodes.iteritems():
            context = settings.COMPRESS_OFFLINE_CONTEXT
            context['compress_forced'] = True
            template._log = log
            template._log_verbosity = verbosity
            for node in nodes:
                compiled_body = env.compile(jinja2.nodes.Template(node.body))
                key = get_offline_hexdigest(Template.from_code(env, compiled_body, {}).render(context))
                try:
                    compiled_node = env.compile(jinja2.nodes.Template([node]))
                    result = Template.from_code(env, compiled_node, {}).render(context)
                    print("%s:%s = %s" % (template.template_name, node.lineno, result))
                except Exception, e:
                    raise CommandError("An error occured during rendering %s: "
                                       "%s" % (template.template_name, e))
                offline_manifest[key] = result
                results.append(result)
                count += 1

        write_offline_manifest(offline_manifest)

        log.write("done\nCompressed %d block(s) from %d template(s).\n" %
                  (count, len(compressor_nodes)))
        return count, results

    def get_nodelist(self, node):
        if (isinstance(node, If) and
                hasattr(node, 'nodelist_true') and
                hasattr(node, 'nodelist_false')):
            return node.nodelist_true + node.nodelist_false
        return getattr(node, "body", getattr(node, "nodes", []))

    def walk_nodes(self, node, block_name=None):
        for node in self.get_nodelist(node):
            if isinstance(node, CallBlock) and isinstance(node.call, Call) and isinstance(node.call.node, ExtensionAttribute)\
            and node.call.node.identifier == 'compressor.contrib.jinja2ext.CompressorExtension':
                yield node
            else:
                for node in self.walk_nodes(node, block_name=block_name):
                    yield node

    def handle_extensions(self, extensions=('html',)):
        """
        organizes multiple extensions that are separated with commas or
        passed by using --extension/-e multiple times.

        for example: running 'django-admin compress -e js,txt -e xhtml -a'
        would result in a extension list: ['.js', '.txt', '.xhtml']

        >>> handle_extensions(['.html', 'html,js,py,py,py,.py', 'py,.py'])
        ['.html', '.js']
        >>> handle_extensions(['.html, txt,.tpl'])
        ['.html', '.tpl', '.txt']
        """
        ext_list = []
        for ext in extensions:
            ext_list.extend(ext.replace(' ', '').split(','))
        for i, ext in enumerate(ext_list):
            if not ext.startswith('.'):
                ext_list[i] = '.%s' % ext_list[i]
        return set(ext_list)

    def handle_noargs(self, **options):
        if not settings.COMPRESS_ENABLED and not options.get("force"):
            raise CommandError(
                "Compressor is disabled. Set the COMPRESS_ENABLED "
                "settting or use --force to override.")
        if not settings.COMPRESS_OFFLINE:
            if not options.get("force"):
                raise CommandError(
                    "Offline compression is disabled. Set "
                    "COMPRESS_OFFLINE or use the --force to override.")
        self.compress(sys.stdout, **options)
