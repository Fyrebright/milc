#!/usr/bin/env python3
# coding=utf-8
import argparse
import logging
import os
import subprocess
import shlex
import sys
from decimal import Decimal
from pathlib import Path
from platform import platform
from tempfile import NamedTemporaryFile

try:
    from ConfigParser import RawConfigParser
except ImportError:
    from configparser import RawConfigParser

try:
    import thread
    import threading
except ImportError:
    thread = None

import argcomplete
import colorama
from appdirs import user_config_dir

from .ansi import MILCFormatter, ansi_colors, ansi_config, ansi_escape, format_ansi
from .configuration import Configuration, SubparserWrapper, get_argument_name, handle_store_boolean
from .attrdict import AttrDict


class MILC(object):
    """MILC - An Opinionated Batteries Included Framework
    """
    def __init__(self):
        """Initialize the MILC object.
        """
        # Setup a lock for thread safety
        self._lock = threading.RLock() if thread else None

        # Define some basic info
        self.acquire_lock()
        self._config_store_true = []
        self._config_store_false = []
        self._description = None
        self._entrypoint = None
        self._inside_context_manager = False
        self.ansi = ansi_colors
        self.arg_only = {}
        self.config = self.config_source = None
        self.config_file = None
        self.default_arguments = {}
        self.platform = platform()

        # Figure out our program name
        self.prog_name = sys.argv[0][:-3] if sys.argv[0].endswith('.py') else sys.argv[0]
        self.prog_name = os.environ.get('MILC_APP_NAME', os.path.basename(self.prog_name))
        self.release_lock()

        # Initialize all the things
        self.initialize_config()
        self.initialize_argparse()
        self.initialize_logging()

    @property
    def description(self):
        return self._description

    @description.setter
    def description(self, value):
        self._description = self._arg_parser.description = value

    def echo(self, text, *args, **kwargs):
        """Print colorized text to stdout.

        ANSI color strings (such as {fg-blue}) will be converted into ANSI
        escape sequences, and the ANSI reset sequence will be added to all
        strings.

        If *args or **kwargs are passed they will be used to %-format the strings.
        """
        if args and kwargs:
            raise RuntimeError('You can only specify *args or **kwargs, not both!')

        args = args or kwargs
        text = format_ansi(text)

        if not self.config.general.color:
            text = ansi_escape.sub('', text)

        print(text % args)

    def run(self, command, capture_output=True, combined_output=False, text=True, **kwargs):
        """Run a command using `subprocess.run`, but using some different defaults.

        Unlike subprocess.run you must supply a sequence of arguments. You can use `shlex.split()` to build this from a string.

        The **kwargs arguments get passed directly to `subprocess.run`.

        Args:
            command
                A sequence where the first item is the command to run, and any remaining items are arguments to pass.

            capture_output
                Set to False to have output written to the terminal instead of being available in the returned `subprocess.CompletedProcess` instance.

            combined_output
                When true STDERR will be written to STDOUT. Equivalent to the shell construct `2>&1`.

            text
                Set to False to disable encoding and get `bytes()` from `.stdout` and `.stderr`.
        """
        # Sanity Checking
        if isinstance(command, str):
            raise TypeError('`command` must be a non-text sequence such as list or tuple.')

        if not capture_output and combined_output:
            raise ValueError("Can't use capture_output=False and combined_output=True at the same time.")

        # On some windows platforms (msys2, possibly others) you have to execute the command through a subshell.
        if 'windows' in self.platform.lower():
            safecmd = map(shlex.quote, command)
            safecmd = ' '.join(safecmd)
            command = [os.environ['SHELL'], '-c', safecmd]

        # Argument Processing
        if capture_output:
            kwargs['stdout'] = subprocess.PIPE
            kwargs['stderr'] = subprocess.PIPE

        if combined_output:
            kwargs['stderr'] = subprocess.STDOUT

        if text:
            kwargs['universal_newlines'] = True

        # Run the command
        self.log.debug('Running command: %s', command)

        return subprocess.run(command, **kwargs)

    def initialize_argparse(self):
        """Prepare to process arguments from sys.argv.
        """
        kwargs = {
            'fromfile_prefix_chars': '@',
            'conflict_handler': 'resolve',
        }

        self.acquire_lock()
        self.subcommands = {}
        self._subparsers = None
        self.argwarn = argcomplete.warn
        self.args = AttrDict()
        self._arg_parser = argparse.ArgumentParser(**kwargs)
        self.set_defaults = self._arg_parser.set_defaults
        self.release_lock()

    def print_help(self, *args, **kwargs):
        """Print a help message for the main program or subcommand, depending on context.
        """
        if self._entrypoint.__name__ in self.subcommands:
            return self.subcommands[self._entrypoint.__name__].print_help(*args, **kwargs)

        return self._arg_parser.print_help(*args, **kwargs)

    def print_usage(self, *args, **kwargs):
        """Print brief description of how the main program or subcommand is invoked, depending on context.
        """
        if self._entrypoint.__name__ in self.subcommands:
            return self.subcommands[self._entrypoint.__name__].print_usage(*args, **kwargs)

        return self._arg_parser.print_usage(*args, **kwargs)

    def completer(self, completer):
        """Add an argcomplete completer to this subcommand.
        """
        self._arg_parser.completer = completer

    def add_argument(self, *args, **kwargs):
        """Wrapper to add arguments and track whether they were passed on the command line.
        """
        if 'action' in kwargs and kwargs['action'] == 'store_boolean':
            return handle_store_boolean(self, *args, **kwargs)

        self.acquire_lock()

        self._arg_parser.add_argument(*args, **kwargs)
        if 'general' not in self.default_arguments:
            self.default_arguments['general'] = {}
        self.default_arguments['general'][get_argument_name(self, *args, **kwargs)] = kwargs.get('default')

        self.release_lock()

    def initialize_logging(self):
        """Prepare the defaults for the logging infrastructure.
        """
        self.acquire_lock()
        self.log_file = None
        self.log_file_mode = 'a'
        self.log_file_handler = None
        self.log_print = True
        self.log_print_to = sys.stderr
        self.log_print_level = logging.INFO
        self.log_file_level = logging.INFO
        self.log_level = logging.INFO
        self.log = logging.getLogger(self.__class__.__name__)
        self.log.setLevel(logging.DEBUG)
        logging.root.setLevel(logging.DEBUG)
        self.release_lock()

        self.add_argument('-V', '--version', version=os.environ.get('MILC_APP_VERSION', 'unknown'), action='version', help='Display the version and exit')
        self.add_argument('-v', '--verbose', action='store_true', help='Make the logging more verbose')
        self.add_argument('--datetime-fmt', default='%Y-%m-%d %H:%M:%S', help='Format string for datetimes')
        self.add_argument('--log-fmt', default='%(levelname)s %(message)s', help='Format string for printed log output')
        self.add_argument('--log-file-fmt', default='[%(levelname)s] [%(asctime)s] [file:%(pathname)s] [line:%(lineno)d] %(message)s', help='Format string for log file.')
        self.add_argument('--log-file-level', default='info', choices=['debug', 'info', 'warning', 'error', 'critical'], help='Logging level for log file.')
        self.add_argument('--log-file', help='File to write log messages to')
        self.add_argument('--color', action='store_boolean', default=ansi_config['color'], help='color in output')
        self.add_argument('--unicode', action='store_boolean', default=ansi_config['unicode'], help='unicode loglevels')
        self.add_argument('--config-file', help='The location for the configuration file')
        self.arg_only['config_file'] = ['general']

    def add_subparsers(self, title='Sub-commands', **kwargs):
        if self._inside_context_manager:
            raise RuntimeError('You must run this before the with statement!')

        self.acquire_lock()
        self._subparsers = self._arg_parser.add_subparsers(title=title, dest='subparsers', **kwargs)
        self.release_lock()

    def acquire_lock(self, blocking=True):
        """Acquire the MILC lock for exclusive access to properties.
        """
        if self._lock:
            self._lock.acquire(blocking)

    def release_lock(self):
        """Release the MILC lock.
        """
        if self._lock:
            self._lock.release()

    def find_config_file(self):
        """Locate the config file.
        """
        if '--config-file' in sys.argv:
            return Path(sys.argv[sys.argv.index('--config-file') + 1]).expanduser().resolve()

        filedir = user_config_dir(appname=self.prog_name, appauthor=os.environ.get('MILC_APP_AUTHOR', self.prog_name.upper()))
        filename = '%s.ini' % self.prog_name

        return Path(filedir, filename).resolve()

    def argument(self, *args, **kwargs):
        """Decorator to call self.add_argument or self.<subcommand>.add_argument.
        """
        if self._inside_context_manager:
            raise RuntimeError('You must run this before the with statement!')

        def argument_function(handler):
            subcommand_name = handler.__name__.replace("_", "-")

            if kwargs.get('arg_only'):
                arg_name = get_argument_name(self, *args, **kwargs)

                if arg_name not in self.arg_only:
                    self.arg_only[arg_name] = []

                self.arg_only[arg_name].append(handler.__name__)
                del kwargs['arg_only']

            if handler is self._entrypoint:
                self.add_argument(*args, **kwargs)

            elif subcommand_name in self.subcommands:
                self.subcommands[subcommand_name].add_argument(*args, **kwargs)

            else:
                raise RuntimeError('Decorated function is not entrypoint or subcommand!')

            return handler

        return argument_function

    def parse_args(self):
        """Parse the CLI args.
        """
        if self.args:
            self.log.debug('Warning: Arguments have already been parsed, ignoring duplicate attempt!')
            return

        argcomplete.autocomplete(self._arg_parser)

        self.acquire_lock()
        for key, value in vars(self._arg_parser.parse_args()).items():
            self.args[key] = value

        if 'entrypoint' in self.args:
            self._entrypoint = self.args.entrypoint

        self.release_lock()

    def read_config_file(self, config_file):
        """Read in the configuration file and return Configuration objects for it and the config_source.
        """
        config = Configuration()
        config_source = Configuration()

        if config_file.exists():
            raw_config = RawConfigParser()
            raw_config.read(str(config_file))

            # Iterate over the config file options and write them into config
            for section in raw_config.sections():
                for option in raw_config.options(section):
                    value = raw_config.get(section, option)

                    # Coerce values into useful datatypes
                    if value.lower() in ['yes', 'true', 'on']:
                        value = True
                    elif value.lower() in ['no', 'false', 'off']:
                        value = False
                    elif value.lower() in ['none']:
                        continue
                    elif value.replace('.', '').isdigit():
                        if '.' in value:
                            value = Decimal(value)
                        else:
                            value = int(value)

                    config[section][option] = value
                    config_source[section][option] = 'config_file'

        return config, config_source

    def initialize_config(self):
        """Read in the configuration file and store it in self.config.
        """
        self.acquire_lock()
        self.config_file = self.find_config_file()
        self.config, self.config_source = self.read_config_file(self.config_file)
        self.release_lock()

    def merge_args_into_config(self):
        """Merge CLI arguments into self.config to create the runtime configuration.
        """
        self.acquire_lock()
        for argument in self.args:
            if argument in ('subparsers', 'entrypoint'):
                continue

            # Find the argument's section
            # Underscores in command's names are converted to dashes during initialization.
            # TODO(Erovia) Find a better solution
            entrypoint_name = self._entrypoint.__name__.replace("_", "-")
            if entrypoint_name in self.default_arguments and argument in self.default_arguments[entrypoint_name]:
                argument_found = True
                section = self._entrypoint.__name__
            if argument in self.default_arguments['general']:
                argument_found = True
                section = 'general'

            if not argument_found:
                raise RuntimeError('Could not find argument in `self.default_arguments`. This should be impossible!')
                exit(1)

            if argument not in self.arg_only or section not in self.arg_only[argument]:
                # Determine the arg value and source
                arg_value = getattr(self.args, argument)
                passed_on_cmdline = False

                if section in self.subcommands:
                    default_value = self.subcommands[section].get_default(argument)
                else:
                    default_value = self._arg_parser.get_default(argument)

                if argument in self._config_store_true and arg_value:
                    passed_on_cmdline = True
                elif argument in self._config_store_false and not arg_value:
                    passed_on_cmdline = True
                elif arg_value is not None:
                    if self.config[section][argument] is None or arg_value != default_value:
                        passed_on_cmdline = True

                # Merge this argument into self.config
                if passed_on_cmdline and (argument in self.default_arguments['general'] or argument in self.default_arguments[entrypoint_name] or argument not in self.config[entrypoint_name]):
                    self.config[section][argument] = arg_value
                    self.config_source[section][argument] = 'argument'

        self.release_lock()

    def _save_config_file(self, config):
        """Write config to disk.
        """
        # Generate a sanitized version of our running configuration
        sane_config = RawConfigParser()
        for section_name, section in config.items():
            sane_config.add_section(section_name)
            for option_name, value in section.items():
                if section_name == 'general':
                    if option_name in ['config_file']:
                        continue
                if value is not None:
                    sane_config.set(section_name, option_name, str(value))

        config_dir = self.config_file.parent
        if not config_dir.exists():
            config_dir.mkdir(parents=True, exist_ok=True)

        # Write the config file atomically.
        self.acquire_lock()
        with NamedTemporaryFile(mode='w', dir=str(config_dir), delete=False) as tmpfile:
            sane_config.write(tmpfile)

        if os.path.getsize(tmpfile.name) > 0:
            os.replace(tmpfile.name, str(self.config_file))
        else:
            self.log.warning('Config file saving failed, not replacing %s with %s.', str(self.config_file), tmpfile.name)
        self.release_lock()

    def write_config_option(self, section, option):
        """Save a single config option to the config file.
        """
        if not self.config_file:
            self.log.warning('%s.config_file not set, not saving config!', self.__class__.__name__)
            return

        config, config_source = self.read_config_file(self.config_file)

        if section in config and option in config[section] and config[section][option] is None:
            del config[section][option]
        else:
            config[section][option] = str(self.config[section][option])

        self._save_config_file(config)

        # Housekeeping
        self.log.info('Wrote configuration to %s', shlex.quote(str(self.config_file)))

    def save_config(self):
        """Save the current configuration to the config file.
        """
        self.log.debug("Saving config file to '%s'", str(self.config_file))

        if not self.config_file:
            self.log.warning('%s.config_file file not set, not saving config!', self.__class__.__name__)
            return

        # Write config to disk
        self._save_config_file(self.config)
        self.log.info('Wrote configuration to %s', shlex.quote(str(self.config_file)))

    def __call__(self):
        """Execute the entrypoint function.
        """
        if not self._inside_context_manager:
            # If they didn't use the context manager use it ourselves
            with self:
                return self.__call__()

        if not self._entrypoint:
            raise RuntimeError('No entrypoint provided!')

        return self._entrypoint(self)

    def entrypoint(self, description):
        """Decorator that marks the entrypoint for simple scripts without subcommands.
        """
        if self._inside_context_manager:
            raise RuntimeError('You must run this before cli()!')

        self.acquire_lock()
        self.description = description
        self.release_lock()

        def entrypoint_func(handler):
            self.acquire_lock()
            self._entrypoint = handler
            self.release_lock()

            return handler

        return entrypoint_func

    def add_subcommand(self, handler, description, hidden=False, **kwargs):
        """Register a subcommand.

        Args:

            handler
                The function to exececute for this subcommand.

            description
                A one-line description to display in --help

            hidden
                When True don't display this command in --help
        """
        if self._inside_context_manager:
            raise RuntimeError('You must run this before the with statement!')

        if self._subparsers is None:
            self.add_subparsers(metavar="")

        name = handler.__name__.replace("_", "-")

        self.acquire_lock()
        if not hidden:
            self._subparsers.metavar = "{%s,%s}" % (self._subparsers.metavar[1:-1], name) if self._subparsers.metavar else "{%s%s}" % (self._subparsers.metavar[1:-1], name)
            kwargs['help'] = description
        self.subcommands[name] = SubparserWrapper(self, name, self._subparsers.add_parser(name, **kwargs))
        self.subcommands[name].set_defaults(entrypoint=handler)

        self.release_lock()

        return handler

    def subcommand(self, description, hidden=False, **kwargs):
        """Decorator to register a subcommand.

        Args:

            description
                A one-line description to display in --help

            hidden
                When True don't display this command in --help
        """
        def subcommand_function(handler):
            return self.add_subcommand(handler, description, hidden=hidden, **kwargs)

        return subcommand_function

    def setup_logging(self):
        """Called by __enter__() to setup the logging configuration.
        """
        if len(logging.root.handlers) != 0:
            # MILC is the only thing that should have root log handlers
            logging.root.handlers = []

        self.acquire_lock()

        if self.config.general.verbose:
            self.log_print_level = logging.DEBUG

        ansi_config['color'] = self.config.general.color
        ansi_config['unicode'] = self.config.general.unicode

        self.log_file = self.config.general.log_file or self.log_file
        self.log_file_format = MILCFormatter(self.config.general.log_file_fmt, self.config.general.datetime_fmt)
        self.log_file_level = getattr(logging, self.config.general.log_file_level.upper())
        self.log_format = MILCFormatter(self.config.general.log_fmt, self.config.general.datetime_fmt)

        if self.log_file:
            self.log_file_handler = logging.FileHandler(self.log_file, self.log_file_mode)
            self.log_file_handler.setLevel(self.log_file_level)
            self.log_file_handler.setFormatter(self.log_file_format)
            logging.root.addHandler(self.log_file_handler)

        if self.log_print:
            self.log_print_handler = logging.StreamHandler(self.log_print_to)
            self.log_print_handler.setLevel(self.log_print_level)
            self.log_print_handler.setFormatter(self.log_format)
            logging.root.addHandler(self.log_print_handler)

        self.release_lock()

    def __enter__(self):
        if self._inside_context_manager:
            self.log.debug('Warning: context manager was entered again. This usually means that self.__call__() was called before the with statement. You probably do not want to do that.')
            return

        self.acquire_lock()
        self._inside_context_manager = True
        self.release_lock()

        colorama.init()
        self.parse_args()
        self.merge_args_into_config()
        self.setup_logging()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.acquire_lock()
        self._inside_context_manager = False
        self.release_lock()

        if exc_type is not None and not isinstance(SystemExit(), exc_type):
            print(exc_type)
            logging.exception(exc_val)
            exit(255)
