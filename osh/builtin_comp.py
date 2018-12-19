#!/usr/bin/python
"""
builtin_comp.py - Completion builtins
"""

import pwd

from core import completion
from core import util
from frontend import args
from frontend import lex
from osh import builtin
from osh import state

from _devbuild.gen import osh_help  # generated file

log = util.log


def _DefineFlags(spec):
  spec.ShortFlag('-F', args.Str, help='Complete with this function')
  spec.ShortFlag('-W', args.Str, help='Complete with these words')
  spec.ShortFlag('-P', args.Str,
      help='Prefix is added at the beginning of each possible completion after '
           'all other options have been applied.')
  spec.ShortFlag('-S', args.Str,
      help='Suffix is appended to each possible completion after '
           'all other options have been applied.')
  spec.ShortFlag('-X', args.Str,
      help='''
A glob pattern to further filter the matches.  It is applied to the list of
possible completions generated by the preceding options and arguments, and each
completion matching filterpat is removed from the list. A leading ! in
filterpat negates the pattern; in this case, any completion not matching
filterpat is removed. 
''')


def _DefineOptions(spec):
  """Common -o options for complete and compgen."""

  # bashdefault, default, filenames, nospace are used in git
  spec.Option(None, 'bashdefault',
      help='If nothing matches, perform default bash completions')
  spec.Option(None, 'default',
      help="If nothing matches, use readline's default filename completion")
  spec.Option(None, 'filenames',
      help="The completion function generates filenames and should be "
           "post-processed")
  spec.Option(None, 'dirnames',
      help="If nothing matches, perform directory name completion")
  spec.Option(None, 'nospace',
      help="Don't append a space to words completed at the end of the line")
  spec.Option(None, 'plusdirs',
      help="After processing the compspec, attempt directory name completion "
      "and return those matches.")


def _DefineActions(spec):
  """Common -A actions for complete and compgen."""

  # NOTE: git-completion.bash uses -f and -v. 
  # My ~/.bashrc on Ubuntu uses -d, -u, -j, -v, -a, -c, -b
  spec.InitActions()
  spec.Action('a', 'alias')
  spec.Action('b', 'binding')
  spec.Action('c', 'command')
  spec.Action('d', 'directory')
  spec.Action('f', 'file')
  spec.Action('j', 'job')
  spec.Action('u', 'user')
  spec.Action('v', 'variable')
  spec.Action(None, 'function')
  spec.Action(None, 'helptopic')  # help
  spec.Action(None, 'setopt')  # set -o
  spec.Action(None, 'shopt')  # shopt -s
  spec.Action(None, 'signal')  # kill -s
  spec.Action(None, 'stopped')


class _SortedWordsAction(object):
  def __init__(self, d):
    self.d = d

  def Matches(self, comp):
    for name in sorted(self.d):
      if name.startswith(comp.to_complete):
        #yield name + ' '  # full word
        yield name


class _DirectoriesAction(object):
  """complete -A directory"""

  def Matches(self, comp):
    raise NotImplementedError('-A directory')


class _UsersAction(object):
  """complete -A user"""

  def Matches(self, comp):
    for u in pwd.getpwall():
      name = u.pw_name
      if name.startswith(comp.to_complete):
        yield name


def _BuildUserSpec(argv, arg, comp_opts, ex):
  """Given flags to complete/compgen, built a UserSpec."""
  actions = []

  # NOTE: bash doesn't actually check the name until completion time, but
  # obviously it's better to check here.
  if arg.F:
    func_name = arg.F
    func = ex.funcs.get(func_name)
    if func is None:
      raise args.UsageError('Function %r not found' % func_name)
    actions.append(completion.ShellFuncAction(ex, func))

  # NOTE: We need completion for -A action itself!!!  bash seems to have it.
  for name in arg.actions:
    if name == 'alias':
      a = _SortedWordsAction(ex.aliases)

    elif name == 'binding':
      # TODO: Where do we get this from?
      a = _SortedWordsAction(['vi-delete'])

    elif name == 'command':
      # compgen -A command in bash is SIX things: aliases, builtins,
      # functions, keywords, external commands relative to the current
      # directory, and external commands in $PATH.

      actions.append(_SortedWordsAction(builtin.BUILTIN_NAMES))
      actions.append(_SortedWordsAction(ex.aliases))
      actions.append(_SortedWordsAction(ex.funcs))
      actions.append(_SortedWordsAction(lex.OSH_KEYWORD_NAMES))
      actions.append(completion.FileSystemAction(exec_only=True))

      # Look on the file system.
      a = completion.ExternalCommandAction(ex.mem)

    elif name == 'directory':
      a = completion.FileSystemAction(dirs_only=True)

    elif name == 'file':
      a = completion.FileSystemAction()

    elif name == 'function':
      a = _SortedWordsAction(ex.funcs)

    elif name == 'job':
      a = _SortedWordsAction(['jobs-not-implemented'])

    elif name == 'user':
      a = _UsersAction()

    elif name == 'variable':
      a = completion.VariablesAction(ex.mem)

    elif name == 'helptopic':
      a = _SortedWordsAction(osh_help.TOPIC_LOOKUP)

    elif name == 'setopt':
      a = _SortedWordsAction(state.SET_OPTION_NAMES)

    elif name == 'shopt':
      a = _SortedWordsAction(state.SHOPT_OPTION_NAMES)

    elif name == 'signal':
      a = _SortedWordsAction(['TODO:signals'])

    elif name == 'stopped':
      a = _SortedWordsAction(['jobs-not-implemented'])

    else:
      raise NotImplementedError(name)

    actions.append(a)

  # e.g. -W comes after -A directory
  if arg.W:
    # TODO: Split with IFS.  Is that done at registration time or completion
    # time?
    actions.append(completion.WordsAction(arg.W.split()))

  if comp_opts.Get('plusdirs'):
    actions.append(completion.FileSystemAction(dirs_only=True))

  # These only happen if there were zero shown.
  else_actions = []
  if comp_opts.Get('default'):
    else_actions.append(completion.FileSystemAction())
  if comp_opts.Get('dirnames'):
    else_actions.append(completion.FileSystemAction(dirs_only=True))

  if not actions and not else_actions:
    raise args.UsageError('No actions defined in completion: %s' % argv)

  # TODO: What about predicate?
  user_spec = completion.UserSpec(
      actions,
      else_actions,
      prefix=arg.P or '',
      suffix=arg.S or '')

  return user_spec


# git-completion.sh uses complete -o and complete -F
COMPLETE_SPEC = args.FlagsAndOptions()

_DefineFlags(COMPLETE_SPEC)
_DefineOptions(COMPLETE_SPEC)
_DefineActions(COMPLETE_SPEC)

COMPLETE_SPEC.ShortFlag('-E',
    help='Define the compspec for an empty line')
COMPLETE_SPEC.ShortFlag('-D',
    help='Define the compspec that applies when nothing else matches')


def Complete(argv, ex, comp_state):
  """complete builtin - register a completion function.

  NOTE: It's a member of Executor because it creates a ShellFuncAction, which
  needs an Executor.
  """
  arg_r = args.Reader(argv)
  arg = COMPLETE_SPEC.Parse(arg_r)
  # TODO: process arg.opt_changes
  #log('arg %s', arg)

  commands = arg_r.Rest()

  if arg.D:
    commands.append('__fallback')  # if the command doesn't match anything
  if arg.E:
    commands.append('__first')  # empty line

  if not commands:
    comp_state.PrintSpecs()
    return 0

  comp_opts = completion.Options(arg.opt_changes)
  user_spec = _BuildUserSpec(argv, arg, comp_opts, ex)
  for command in commands:
    comp_state.RegisterName(command, comp_opts, user_spec)

  patterns = []
  for pat in patterns:
    comp_state.RegisterGlob(pat, comp_opts, user_spec)

  return 0


COMPGEN_SPEC = args.FlagsAndOptions()  # for -o and -A

# TODO: Add -l for COMP_LINE.  -p for COMP_POINT ?
_DefineFlags(COMPGEN_SPEC)
_DefineOptions(COMPGEN_SPEC)
_DefineActions(COMPGEN_SPEC)


def CompGen(argv, ex):
  """Print completions on stdout."""

  arg_r = args.Reader(argv)
  arg = COMPGEN_SPEC.Parse(arg_r)

  if arg_r.AtEnd():
    to_complete = ''
  else:
    to_complete = arg_r.Peek()
    arg_r.Next()
    # bash allows extra arguments here.
    #if not arg_r.AtEnd():
    #  raise args.UsageError('Extra arguments')

  matched = False

  comp_opts = completion.Options(arg.opt_changes)
  user_spec = _BuildUserSpec(argv, arg, comp_opts, ex)

  # NOTE: Matching bash in passing dummy values for COMP_WORDS and COMP_CWORD,
  # and also showing ALL COMPREPLY reuslts, not just the ones that start with
  # the word to complete.
  matched = False 
  comp = completion.Api()
  comp.Update(words=['compgen', to_complete], index=-1,
              to_complete=to_complete)
  for m in user_spec.Matches(comp):
    matched = True
    print(m)

  # TODO:
  # - need to dedupe results.

  return 0 if matched else 1


COMPOPT_SPEC = args.FlagsAndOptions()  # for -o
_DefineOptions(COMPOPT_SPEC)


def CompOpt(argv, comp_state):
  arg_r = args.Reader(argv)
  arg = COMPOPT_SPEC.Parse(arg_r)

  if not comp_state.currently_completing:
    # bash checks this.
    util.error('compopt: not currently executing a completion function')
    return 1

  comp_opts = completion.Options(arg.opt_changes)

  # NOTE: This is supposed to fail if a completion isn't being generated?
  # The executor should have a mode?

  #log('compopt: %s', arg)
  log('compopt %s', comp_opts)
  return 0
