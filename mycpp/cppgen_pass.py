"""
cppgen.py - AST pass to that prints C++ code
"""
import io
import json  # for "C escaping"
import sys

from typing import overload, Union, Optional, Any, Dict

from mypy.visitor import ExpressionVisitor, StatementVisitor
from mypy.types import (
    Type, AnyType, NoneTyp, TupleType, Instance, Overloaded, CallableType,
    UnionType, UninhabitedType, PartialType)
from mypy.nodes import (
    Expression, Statement, NameExpr, IndexExpr, MemberExpr, TupleExpr,
    ExpressionStmt, AssignmentStmt, StrExpr, SliceExpr, FuncDef,
    ComparisonExpr, CallExpr, IntExpr)

import format_strings
from crash import catch_errors
from util import log


T = None

class UnsupportedException(Exception):
    pass


def get_c_type(t):
  if isinstance(t, NoneTyp):  # e.g. a function that doesn't return anything
    return 'void'

  if isinstance(t, AnyType):
    # Note: this usually results in another compile-time error.  We should get
    # rid of the 'Any' types.
    return 'void*'

  # TODO: It seems better not to check for string equality, but that's what
  # mypyc/genops.py does?

  if isinstance(t, Instance):
    type_name = t.type.fullname()

    if type_name == 'builtins.int':
      c_type = 'int'

    elif type_name == 'builtins.bool':
      c_type = 'bool'

    elif type_name == 'builtins.str':
      c_type = 'Str*'

    elif type_name == 'builtins.list':
      assert len(t.args) == 1, t.args
      type_param = t.args[0]
      inner_c_type = get_c_type(type_param)
      c_type = 'List<%s>*' % inner_c_type

    elif type_name == 'builtins.dict':
      params = []
      for type_param in t.args:
        params.append(get_c_type(type_param))
      c_type = 'Dict<%s>*' % ', '.join(params)

    # TODO: we might want Writer and LineReader base classes, and
    # mylib::Writer
    #   CFileWriter
    #   BufWriter

    elif type_name == 'typing.IO':
      c_type = 'mylib::File*'

    else:
      # fullname() => 'parse.Lexer'; name() => 'Lexer'

      # NOTE: It would be nice to leave off the namespace if we're IN that
      # namespace.  But that is cosmetic.

      # Check base class for runtime.SimpleObj so we can output
      # expr_asdl::tok_t instead of expr_asdl::tok_t*.  That is a enum, while
      # expr_t is a "regular base class".
      # NOTE: Could we avoid the typedef?  If it's SimpleObj, just generate
      # tok_e instead?

      base_class_names = [b.type.fullname() for b in t.type.bases]
      #log('** base_class_names %s', base_class_names)
      # not sure why this isn't runtime.SimpleObj
      if 'asdl.pybase.SimpleObj' in base_class_names:
        is_pointer = ''
      else:
        is_pointer = '*'

      parts = t.type.fullname().split('.')
      c_type = '%s::%s%s' % (parts[-2], parts[-1], is_pointer)

  elif isinstance(t, PartialType):
    # For Any?
    c_type = 'void*'

  elif isinstance(t, UninhabitedType):
    # UninhabitedType has a NoReturn flag
    c_type = 'void'

  elif isinstance(t, TupleType):
    inner_c_types = []
    for inner_type in t.items:
      inner_c_types.append(get_c_type(inner_type))

    c_type = 'Tuple%d<%s>*' % (len(t.items), ', '.join(inner_c_types))

  elif isinstance(t, UnionType):
    # Special case for Optional[T] == Union[T, None]
    if len(t.items) != 2:
      raise NotImplementedError('Expected Optional, got %s' % t)

    if not isinstance(t.items[1], NoneTyp):
      raise NotImplementedError('Expected Optional, got %s' % t)

    c_type = get_c_type(t.items[0])

  elif isinstance(t, CallableType):
    # Function types are expanded
    # Callable[[Parser, Token, int], arith_expr_t] =>
    # arith_expr_t* (*f)(Parser*, Token*, int) nud;

    ret_type = get_c_type(t.ret_type)
    arg_types = [get_c_type(typ) for typ in t.arg_types]
    c_type = '%s (*f)(%s)' % (ret_type, ', '.join(arg_types))

  else:
    raise NotImplementedError('MyPy type: %s %s' % (type(t), t))

  return c_type


class Generate(ExpressionVisitor[T], StatementVisitor[None]):

    def __init__(self, types: Dict[Expression, Type], const_lookup, f,
                 virtual=None, local_vars=None,
                 decl=False, forward_decl=False):
      self.types = types
      self.const_lookup = const_lookup
      self.f = f 

      self.virtual = virtual
      # local_vars: FuncDef node -> list of type, var
      # This is different from member_vars because we collect it in the 'decl'
      # phase.  But then write it in the definition phase.
      self.local_vars = local_vars
      self.fmt_funcs = io.StringIO()

      self.decl = decl
      self.forward_decl = forward_decl

      self.unique_id = 0

      self.indent = 0
      self.local_var_list = []  # Collected at assignment
      self.prepend_to_block = None  # For writing vars after {
      self.in_func_body = False

      # This is cleared when we start visiting a class.  Then we visit all the
      # methods, and accumulate the types of everything that looks like
      # self.foo = 1.  Then we write C++ class member declarations at the end
      # of the class.
      # This is all in the 'decl' phase.
      self.member_vars = {}  # type: Dict[str, Type]
      self.current_class_name = None  # for prototypes

      self.imported_names = set()  # For module::Foo() vs. self.foo

    def log(self, msg, *args):
      ind_str = self.indent * '  '
      log(ind_str + msg, *args)

    def write(self, msg, *args):
      if self.decl or self.forward_decl:
        return
      if args:
        msg = msg % args
      self.f.write(msg)

    # Write respecting indent
    def write_ind(self, msg, *args):
      if self.decl or self.forward_decl:
        return
      ind_str = self.indent * '  '
      if args:
        msg = msg % args
      self.f.write(ind_str + msg)

    # A little hack to reuse this pass for declarations too
    def decl_write(self, msg, *args):
      if args:
        msg = msg % args
      self.f.write(msg)

    def decl_write_ind(self, msg, *args):
      ind_str = self.indent * '  '
      if args:
        msg = msg % args
      self.f.write(ind_str + msg)


    #
    # COPIED from IRBuilder
    #

    @overload
    def accept(self, node: Expression) -> T: ...

    @overload
    def accept(self, node: Statement) -> None: ...

    def accept(self, node: Union[Statement, Expression]) -> Optional[T]:
        with catch_errors(self.module_path, node.line):
            if isinstance(node, Expression):
                try:
                    res = node.accept(self)
                    #res = self.coerce(res, self.node_type(node), node.line)

                # If we hit an error during compilation, we want to
                # keep trying, so we can produce more error
                # messages. Generate a temp of the right type to keep
                # from causing more downstream trouble.
                except UnsupportedException:
                    res = self.alloc_temp(self.node_type(node))
                return res
            else:
                try:
                    node.accept(self)
                except UnsupportedException:
                    pass
                return None

    # Not in superclasses:

    def visit_mypy_file(self, o: 'mypy.nodes.MypyFile') -> T:
        # Skip some stdlib stuff.  A lot of it is brought in by 'import
        # typing'.
        if o.fullname() in (
            '__future__', 'sys', 'types', 'typing', 'abc', '_ast', 'ast',
            '_weakrefset', 'collections', 'cStringIO', 're', 'builtins'):

            # These module are special; their contents are currently all
            # built-in primitives.
            return

        self.log('')
        self.log('mypyfile %s', o.fullname())

        mod_parts = o.fullname().split('.')
        if self.forward_decl:
          comment = 'forward declare' 
        elif self.decl:
          comment = 'declare' 
        else:
          comment = 'define'

        self.decl_write_ind('namespace %s {  // %s\n', mod_parts[-1], comment)

        self.module_path = o.path

        if self.forward_decl:
          self.indent += 1

        self.log('defs %s', o.defs)
        for node in o.defs:
          # skip module docstring
          if (isinstance(node, ExpressionStmt) and
              isinstance(node.expr, StrExpr)):
              continue
          self.accept(node)

        # Write fmtX() functions inside the namespace.
        if self.decl:
          self.decl_write(self.fmt_funcs.getvalue())
          self.fmt_funcs = io.StringIO()  # clear it for the next file

        if self.forward_decl:
          self.indent -= 1

        self.decl_write('\n')
        self.decl_write_ind(
            '}  // %s namespace %s\n', comment, mod_parts[-1])
        self.decl_write('\n')


    # NOTE: Copied ExpressionVisitor and StatementVisitor nodes below!

    # LITERALS

    def visit_int_expr(self, o: 'mypy.nodes.IntExpr') -> T:
        self.write(str(o.value))

    def visit_str_expr(self, o: 'mypy.nodes.StrExpr') -> T:
        self.write(self.const_lookup[o])

    def visit_bytes_expr(self, o: 'mypy.nodes.BytesExpr') -> T:
        pass

    def visit_unicode_expr(self, o: 'mypy.nodes.UnicodeExpr') -> T:
        pass

    def visit_float_expr(self, o: 'mypy.nodes.FloatExpr') -> T:
        pass

    def visit_complex_expr(self, o: 'mypy.nodes.ComplexExpr') -> T:
        pass

    # Expressions

    def visit_ellipsis(self, o: 'mypy.nodes.EllipsisExpr') -> T:
        pass

    def visit_star_expr(self, o: 'mypy.nodes.StarExpr') -> T:
        pass

    def visit_name_expr(self, o: 'mypy.nodes.NameExpr') -> T:
        if o.name == 'None':
          self.write('nullptr')
          return
        if o.name == 'True':
          self.write('true')
          return
        if o.name == 'False':
          self.write('false')
          return
        if o.name == 'self':
          self.write('this')
          return

        self.write(o.name)

    def visit_member_expr(self, o: 'mypy.nodes.MemberExpr') -> T:
        t = self.types[o]
        if o.expr:  
          #log('member o = %s', o)

          # This is an approximate hack that assumes that locals don't shadow
          # imported names.  Might be a problem with names like 'word'?
          if (isinstance(o.expr, NameExpr) and (
              o.expr.name in self.imported_names or
              o.expr.name == 'mylib' or
              o.name == '__init__'
              )):
            op = '::'
          else:
            op = '->'  # Everything is a pointer

          self.accept(o.expr)
          self.write(op)
        self.write('%s', o.name)

    def visit_yield_from_expr(self, o: 'mypy.nodes.YieldFromExpr') -> T:
        pass

    def visit_yield_expr(self, o: 'mypy.nodes.YieldExpr') -> T:
        pass

    def visit_call_expr(self, o: 'mypy.nodes.CallExpr') -> T:
        if o.callee.name == 'isinstance':
          assert len(o.args) == 2, args
          obj = o.args[0]
          typ = o.args[1]

          if 0:
            log('obj %s', obj)
            log('typ %s', typ)

          self.accept(obj)
          self.write('->tag == ')
          assert isinstance(typ, NameExpr), typ

          # source__CFlag -> source_e::CFlag
          tag = typ.name.replace('__', '_e::')
          self.write(tag)
          return

        # HACK for log("%s", s)
        printf_style = False
        if o.callee.name == 'log':
          printf_style = True

        callee_name = o.callee.name
        callee_type = self.types[o.callee]

        # e.g. int() takes str, float, etc.  It doesn't matter for translation.
        if isinstance(callee_type, Overloaded):
          if 0:
            for item in callee_type.items():
              self.log('item: %s', item)

        if isinstance(callee_type, CallableType):
          # If the function name is the same as the return type, then add 'new'.
          # f = Foo() => f = new Foo().
          ret_type = callee_type.ret_type
          # str(i) doesn't need new.  For now it's a free function.
          # TODO: rename int_to_str?  or Str::from_int()?
          if (callee_name not in ('str',) and 
              isinstance(ret_type, Instance) and 
              callee_name == ret_type.type.name()):
            self.write('new ')

        # Namespace.
        if callee_name == 'int':  # int('foo') in Python conflicts with keyword
          self.write('str_to_int')
        else:
          self.accept(o.callee)  # could be f() or obj.method()

        self.write('(')
        for i, arg in enumerate(o.args):
          if i != 0:
            self.write(', ')
          self.accept(arg)

          # Add ->data_ to string arguments after the first one
          if printf_style and i != 0:
            typ = self.types[arg]
            # for Optional[Str]
            if isinstance(typ, UnionType):
              t = typ.items[0]
            else:
              t = typ
            if t.type.fullname() == 'builtins.str':
              self.write('->data_')

        self.write(')')

        # TODO: look at keyword arguments!
        #self.log('  arg_kinds %s', o.arg_kinds)
        #self.log('  arg_names %s', o.arg_names)

    def visit_op_expr(self, o: 'mypy.nodes.OpExpr') -> T:
        c_op = o.op

        # a + b when a and b are strings.  (Can't use operator overloading
        # because they're pointers.)
        left_type = self.types[o.left]
        right_type = self.types[o.right]

        # NOTE: Need get_c_type to handle Optional[Str*] in ASDL schemas.
        # Could tighten it up later.
        left_ctype = get_c_type(left_type)
        right_ctype = get_c_type(right_type)

        #if c_op == '+':
        if 0:
          self.log('*** %r', c_op)
          self.log('%s', o.left)
          self.log('%s', o.right)
          #self.log('t0 %r', t0.type.fullname())
          #self.log('t1 %r', t1.type.fullname())
          self.log('left_ctype %r', left_ctype)
          self.log('right_ctype %r', right_ctype)
          self.log('')

        if left_ctype == right_ctype == 'Str*' and c_op == '+':
          self.write('str_concat(')
          self.accept(o.left)
          self.write(', ')
          self.accept(o.right)
          self.write(')')
          return

        if left_ctype == 'Str*' and right_ctype == 'int' and c_op == '*':
          self.write('str_repeat(')
          self.accept(o.left)
          self.write(', ')
          self.accept(o.right)
          self.write(')')
          return

        # RHS can be primitive or tuple
        if left_ctype == 'Str*' and c_op == '%':
          if not isinstance(o.left, StrExpr):
            raise AssertionError('Expected constant format string, got %s' % o.left)
          fmt = o.left.value

          parts = format_strings.Parse(fmt)

          temp_name = 'fmt%d' % self.unique_id
          self.unique_id += 1

          # Write a buffer with fmtX() functions.

          if self.decl:
            self.fmt_funcs.write('Str* %s(' % temp_name)

            #log('right_type %s', right_type)
            if isinstance(right_type, Instance):
              self.fmt_funcs.write('%s a0' % right_ctype)
            elif isinstance(right_type, TupleType):
              for i, typ in enumerate(right_type.items):
                if i != 0:
                  self.fmt_funcs.write(', ');
                self.fmt_funcs.write('%s a%d' % (get_c_type(typ), i))

            # Handle Optional[str]
            elif (isinstance(right_type, UnionType) and
                  len(right_type.items) == 2 and
                  isinstance(right_type.items[1], NoneTyp)):
              self.fmt_funcs.write('%s a0' % get_c_type(right_type.items[0]))
            else:
              raise AssertionError(right_type)

            self.fmt_funcs.write(') {\n')
            self.fmt_funcs.write('  gBuf.clear();\n')

            for part in parts:
              if isinstance(part, format_strings.LiteralPart):
                # JSON does a decent job of escaping for now.
                escaped = json.dumps(part.s)
                self.fmt_funcs.write(
                    '  gBuf.write_const(%s, %d);\n' % (escaped, part.strlen))
              elif isinstance(part, format_strings.SubstPart):
                self.fmt_funcs.write(
                    '  gBuf.format_%s(a%d);\n' %
                    (part.char_code, part.arg_num))
              else:
                raise AssertionError(part)

            self.fmt_funcs.write('  return gBuf.getvalue();\n')
            self.fmt_funcs.write('}\n')
            self.fmt_funcs.write('\n')

          # In the definition pass, write the call site.
          self.write('%s(' % temp_name)
          if isinstance(right_type, TupleType):
            for i, item in enumerate(o.right.items):
              if i != 0:
                self.write(', ')
              self.accept(item)
          else:
            self.accept(o.right)

          self.write(')')
          return

        self.accept(o.left)
        self.write(' %s ', c_op)
        self.accept(o.right)

    def visit_comparison_expr(self, o: 'mypy.nodes.ComparisonExpr') -> T:
        # Make sure it's binary
        assert len(o.operators) == 1, o.operators
        assert len(o.operands) == 2, o.operands

        operator = o.operators[0]
        left = o.operands[0]
        right = o.operands[1]

        # Assume is and is not are for None / nullptr comparison.
        if operator == 'is':  # foo is None => foo == nullptr
          self.accept(o.operands[0])
          self.write(' == ')
          self.accept(o.operands[1])
          return

        if operator == 'is not':  # foo is not None => foo != nullptr
          self.accept(o.operands[0])
          self.write(' != ')
          self.accept(o.operands[1])
          return

        # TODO: Change Optional[T] to T for our purposes?
        t0 = self.types[left]
        t1 = self.types[right]

        # 0: not a special case
        # 1: str
        # 2: Optional[str] which is Union[str, None]
        left_type = 0  # not a special case
        right_type = 0  # not a special case
        if isinstance(t0, Instance) and t0.type.fullname() == 'builtins.str':
          left_type = 1
        if isinstance(t1, Instance) and t1.type.fullname() == 'builtins.str':
          right_type = 1

        if (isinstance(t0, UnionType) and len(t0.items) == 2 and
            isinstance(t0.items[1], NoneTyp)):
            left_type = 2
        if (isinstance(t1, UnionType) and len(t1.items) == 2 and
            isinstance(t1.items[1], NoneTyp)):
            right_type = 2

        if left_type > 0 and right_type > 0 and operator in ('==', '!='):
          if operator == '!=':
            self.write('!(')

          # NOTE: This could also be str_equals(left, right)?  Does it make a
          # difference?
          if left_type > 1 or right_type > 1:
            self.write('maybe_str_equals(')
          else:
            self.write('str_equals(')
          self.accept(left)
          self.write(', ')
          self.accept(right)
          self.write(')')

          if operator == '!=':
            self.write(')')
          return

        if operator == 'in':
          # x in mylist => mylist->contains(x) 
          self.accept(right)
          self.write('->contains(')
          self.accept(left)
          self.write(')')
          return

        if operator == 'not in':
          # x not in mylist => !(mylist->contains(x))
          self.write('!(')
          self.accept(right)
          self.write('->contains(')
          self.accept(left)
          self.write('))')
          return

        # Default case
        self.accept(o.operands[0])
        self.write(' %s ', o.operators[0])
        self.accept(o.operands[1])

    def visit_cast_expr(self, o: 'mypy.nodes.CastExpr') -> T:
        pass

    def visit_reveal_expr(self, o: 'mypy.nodes.RevealExpr') -> T:
        pass

    def visit_super_expr(self, o: 'mypy.nodes.SuperExpr') -> T:
        pass

    def visit_assignment_expr(self, o: 'mypy.nodes.AssignmentExpr') -> T:
        pass

    def visit_unary_expr(self, o: 'mypy.nodes.UnaryExpr') -> T:
        # e.g. a[-1] or 'not x'
        if o.op == 'not':
          op_str = '!'
        else:
          op_str = o.op
        self.write(op_str)
        self.accept(o.expr)

    def visit_list_expr(self, o: 'mypy.nodes.ListExpr') -> T:
        list_type = self.types[o]
        #self.log('**** list_type = %s', list_type)
        c_type = get_c_type(list_type)
        assert c_type.endswith('*'), c_type
        c_type = c_type[:-1]  # HACK TO CLEAN UP

        if len(o.items) == 0:
            self.write('new %s()' % c_type)
        else:
            # Use initialize list.  Lists are MUTABLE so we can't pull them to
            # the top level.
            self.write('new %s({' % c_type)
            for i, item in enumerate(o.items):
                if i != 0:
                    self.write(', ')
                self.accept(item)
                # TODO: const_lookup
            self.write('})')

    def visit_dict_expr(self, o: 'mypy.nodes.DictExpr') -> T:
        dict_type = self.types[o]
        c_type = get_c_type(dict_type)
        assert c_type.endswith('*'), c_type
        c_type = c_type[:-1]  # HACK TO CLEAN UP

        self.write('new %s(' % c_type)
        if o.items:
          self.write('{')
          for i, item in enumerate(o.items):
            # TODO:  we can use an initializer list, I think.
            pass
          self.write('}')
        self.write(')')

    def visit_tuple_expr(self, o: 'mypy.nodes.TupleExpr') -> T:
        tuple_type = self.types[o]
        c_type = get_c_type(tuple_type)
        assert c_type.endswith('*'), c_type
        c_type = c_type[:-1]  # HACK TO CLEAN UP

        if len(o.items) == 0:
            self.write('new %s()' % c_type)
        else:
            # Use initialize list.  Lists are MUTABLE so we can't pull them to
            # the top level.
            self.write('new %s(' % c_type)
            for i, item in enumerate(o.items):
                if i != 0:
                    self.write(', ')
                self.accept(item)
                # TODO: const_lookup
            self.write(')')

    def visit_set_expr(self, o: 'mypy.nodes.SetExpr') -> T:
        pass

    def visit_index_expr(self, o: 'mypy.nodes.IndexExpr') -> T:
        self.accept(o.base)

        #base_type = self.types[o.base]
        #self.log('*** BASE TYPE %s', base_type)

        if isinstance(o.index, SliceExpr):
          self.accept(o.index)  # method call
        else:
          # it's hard syntactically to do (*a)[0], so do it this way.
          self.write('->index(')
          self.accept(o.index)
          self.write(')')

    def visit_type_application(self, o: 'mypy.nodes.TypeApplication') -> T:
        pass

    def visit_lambda_expr(self, o: 'mypy.nodes.LambdaExpr') -> T:
        pass

    def visit_list_comprehension(self, o: 'mypy.nodes.ListComprehension') -> T:
        pass

    def visit_set_comprehension(self, o: 'mypy.nodes.SetComprehension') -> T:
        pass

    def visit_dictionary_comprehension(self, o: 'mypy.nodes.DictionaryComprehension') -> T:
        pass

    def visit_generator_expr(self, o: 'mypy.nodes.GeneratorExpr') -> T:
        pass

    def visit_slice_expr(self, o: 'mypy.nodes.SliceExpr') -> T:
        self.write('->slice(')
        if o.begin_index:
          self.accept(o.begin_index)
        else: 
          self.write('0')  # implicit begining

        if o.end_index:
          self.write(', ')
          self.accept(o.end_index)
        self.write(')')

        if o.stride:
          raise AssertionError('Stride not supported')

    def visit_conditional_expr(self, o: 'mypy.nodes.ConditionalExpr') -> T:
        # 0 if b else 1 -> b ? 0 : 1
        self.accept(o.cond)
        self.write(' ? ')
        self.accept(o.if_expr)
        self.write(' : ')
        self.accept(o.else_expr)

    def visit_backquote_expr(self, o: 'mypy.nodes.BackquoteExpr') -> T:
        pass

    def visit_type_var_expr(self, o: 'mypy.nodes.TypeVarExpr') -> T:
        pass

    def visit_type_alias_expr(self, o: 'mypy.nodes.TypeAliasExpr') -> T:
        pass

    def visit_namedtuple_expr(self, o: 'mypy.nodes.NamedTupleExpr') -> T:
        pass

    def visit_enum_call_expr(self, o: 'mypy.nodes.EnumCallExpr') -> T:
        pass

    def visit_typeddict_expr(self, o: 'mypy.nodes.TypedDictExpr') -> T:
        pass

    def visit_newtype_expr(self, o: 'mypy.nodes.NewTypeExpr') -> T:
        pass

    def visit__promote_expr(self, o: 'mypy.nodes.PromoteExpr') -> T:
        pass

    def visit_await_expr(self, o: 'mypy.nodes.AwaitExpr') -> T:
        pass

    def visit_temp_node(self, o: 'mypy.nodes.TempNode') -> T:
        pass

    def _write_tuple_unpacking(self, temp_name, lval_items, item_types):
      """Used by assignment and for loops."""
      for i, (lval_item, item_type) in enumerate(zip(lval_items, item_types)):
        #self.log('*** %s :: %s', lval_item, item_type)
        if isinstance(lval_item, NameExpr):
          if lval_item.name == '_':
            continue

          item_c_type = get_c_type(item_type)
          # declare it at the top of the function
          if self.decl:
            self.local_var_list.append((lval_item.name, item_c_type))
          self.write_ind('%s', lval_item.name)
        else:
          # Could be MemberExpr like self.foo, self.bar = baz
          self.write_ind('')
          self.accept(lval_item)

        self.write(' = %s->at%d();\n', temp_name, i)  # RHS

    def visit_assignment_stmt(self, o: 'mypy.nodes.AssignmentStmt') -> T:
        # I think there are more than one when you do a = b = 1, which I never
        # use.
        assert len(o.lvalues) == 1, o.lvalues
        lval = o.lvalues[0]

        #    src = cast(source__SourcedFile, src)
        # -> source__SourcedFile* src = static_cast<source__SourcedFile>(src)
        if isinstance(o.rvalue, CallExpr) and o.rvalue.callee.name == 'cast':
          assert isinstance(lval, NameExpr)
          call = o.rvalue
          type_expr = call.args[0]
          if isinstance(type_expr, MemberExpr):
            subtype_name = '%s::%s' % (type_expr.expr.name, type_expr.name)
          else:
            subtype_name = type_expr.name

          # Hack for now
          if subtype_name != 'int':
            subtype_name += '*'

          self.write_ind(
              '%s %s = static_cast<%s>(', subtype_name, lval.name,
              subtype_name)
          self.accept(call.args[1])  # variable being casted
          self.write(');\n')
          return

        if isinstance(lval, NameExpr):
          if lval.name == '_':  # Skip _ = log
            return

          lval_type = self.types[lval]
          c_type = get_c_type(lval_type)

          # for "hoisting" to the top of the function
          if self.in_func_body:
            self.write_ind('%s = ', lval.name)
            if self.decl:
              self.local_var_list.append((lval.name, c_type))
          else:
            # globals always get a type -- they're not mutated
            self.write_ind('%s %s = ', c_type, lval.name)

          self.accept(o.rvalue)
          self.write(';\n')

        elif isinstance(lval, MemberExpr):
          self.write_ind('')
          self.accept(lval)
          self.write(' = ')
          self.accept(o.rvalue)
          self.write(';\n')

          # Collect statements that look like self.foo = 1
          if isinstance(lval.expr, NameExpr) and lval.expr.name == 'self':
            log('    lval.name %s', lval.name)
            lval_type = self.types[lval]
            self.member_vars[lval.name] = lval_type

        elif isinstance(lval, IndexExpr):  # a[x] = 1
          self.write_ind('(*')
          self.accept(lval.base)
          self.write(')[')
          self.accept(lval.index)
          self.write('] = ')
          self.accept(o.rvalue)
          self.write(';\n')

        elif isinstance(lval, TupleExpr):
          # An assignment to an n-tuple turns into n+1 statements.  Example:
          #
          # x, y = mytuple
          #
          # Tuple2<int, Str*> tup1 = mytuple
          # int x = tup1->at0()
          # Str* y = tup1->at1()

          rvalue_type = self.types[o.rvalue]
          c_type = get_c_type(rvalue_type)

          temp_name = 'tup%d' % self.unique_id
          self.unique_id += 1
          self.write_ind('%s %s = ', c_type, temp_name)

          self.accept(o.rvalue)
          self.write(';\n')

          self._write_tuple_unpacking(temp_name, lval.items, rvalue_type.items)
        else:
          raise AssertionError(lval)

    def visit_for_stmt(self, o: 'mypy.nodes.ForStmt') -> T:
        self.log('ForStmt')
        self.log('  index_type %s', o.index_type)
        self.log('  inferred_item_type %s', o.inferred_item_type)
        self.log('  inferred_iterator_type %s', o.inferred_iterator_type)

        func_name = None  # does the loop look like 'for x in func():' ?
        if isinstance(o.expr, CallExpr) and isinstance(o.expr.callee, NameExpr):
          func_name = o.expr.callee.name

        # special case: 'for i in xrange(3)'
        if func_name == 'xrange':
          index_name = o.index.name
          args = o.expr.args
          num_args = len(args)

          if num_args == 1:  # xrange(end)
            self.write_ind('for (int %s = 0; %s < ', index_name, index_name)
            self.accept(args[0])
            self.write('; ++%s) ', index_name)

            self.accept(o.body)
            return

          elif num_args == 2:  # xrange(being, end)
            self.write_ind('for (int %s = ', index_name)
            self.accept(args[0])
            self.write('; %s < ', index_name)
            self.accept(args[1])
            self.write('; ++%s) ', index_name)

            self.accept(o.body)
            return

          else:
            raise AssertionError

        # for i, x in enumerate(...):
        index0_name = None
        if func_name == 'enumerate':
          assert isinstance(o.index, TupleExpr), o.index
          index0 = o.index.items[0]
          assert isinstance(index0, NameExpr), index0
          index0_name = index0.name  # generate int i = 0; ; ++i

          # type of 'x' in 'for i, x in enumerate(...)'
          item_type = o.inferred_item_type.items[1] 
          index_expr = o.index.items[1]

          # enumerate(mylist) turns into iteration over mylist with variable i
          assert len(o.expr.args) == 1, o.expr.args
          iterated_over = o.expr.args[0]
        else:
          item_type = o.inferred_item_type
          index_expr = o.index
          iterated_over = o.expr

        over_type = self.types[iterated_over]
        self.log('  iterating over type %s', over_type)

        if over_type.type.fullname() == 'builtins.list':
          c_type = get_c_type(over_type)
          assert c_type.endswith('*'), c_type
          c_iter_type = c_type.replace('List', 'ListIter')[:-1]  # remove *
        else:
          c_iter_type = 'StrIter'

        if index0_name:
          # can't initialize two things in a for loop, so do it on a separate line
          if self.decl:
            self.local_var_list.append((index0_name, 'int'))
          self.write_ind('%s = 0;\n', index0_name)
          index_update = ', ++%s' % index0_name
        else:
          index_update = ''

        self.write_ind('for (%s it(', c_iter_type)
        self.accept(iterated_over)  # the thing being iterated over
        self.write('); !it.Done(); it.Next()%s) {\n', index_update)

        # for x in it: ...
        # for i, x in enumerate(pairs): ...
        if isinstance(item_type, Instance) or index0_name:
          c_item_type = get_c_type(item_type)
          self.write_ind('  %s ', c_item_type)
          self.accept(index_expr)
          self.write(' = it.Value();\n')

        elif isinstance(item_type, TupleType):  # for x, y in pairs
          # Example:
          # for (ListIter it(mylist); !it.Done(); it.Next()) {
          #   Tuple2<int, Str*> tup1 = it.Value();
          #   int i = tup1->at0();
          #   Str* s = tup1->at1();
          #   log("%d %s", i, s);
          # }

          temp_name = 'tup%d' % self.unique_id
          self.unique_id += 1
          c_item_type = get_c_type(item_type)
          self.write_ind('  %s %s = it.Value();\n', c_item_type, temp_name)

          assert isinstance(o.index, TupleExpr)
          self.indent += 1

          self._write_tuple_unpacking(
              temp_name, o.index.items, item_type.items)

          self.indent -= 1

        else:
          raise AssertionError('Unexpected type %s' % item_type)

        # Copy of visit_block, without opening {
        self.indent += 1
        block = o.body
        for stmt in block.body:
            # Ignore things that look like docstrings
            if isinstance(stmt, ExpressionStmt) and isinstance(stmt.expr, StrExpr):
                continue

            #log('-- %d', self.indent)
            self.accept(stmt)
        self.indent -= 1
        self.write_ind('}\n')

        if o.else_body:
          self.accept(o.else_body)

    def visit_with_stmt(self, o: 'mypy.nodes.WithStmt') -> T:
        pass

    def visit_del_stmt(self, o: 'mypy.nodes.DelStmt') -> T:
        pass

    def _write_func_args(self, o: 'mypy.nodes.FuncDef'):
        first = True
        for i, (arg_type, arg) in enumerate(zip(o.type.arg_types, o.arguments)):
          if not first:
            self.decl_write(', ')

          c_type = get_c_type(arg_type)
          arg_name = arg.variable.name()

          # C++ has implicit 'this'
          if arg_name == 'self':
            continue

          self.decl_write('%s %s', c_type, arg_name)
          first = False

          # We can't use __str__ on these Argument objects?  That seems like an
          # oversight
          #self.log('%r', arg)

          if 0:
            self.log('Argument %s', arg.variable)
            self.log('  type_annotation %s', arg.type_annotation)
            # I think these are for default values
            self.log('  initializer %s', arg.initializer)
            self.log('  kind %s', arg.kind)

    def visit_func_def(self, o: 'mypy.nodes.FuncDef') -> T:
        # Skip these for now
        if o.name() == '__repr__':
          return

        # No function prototypes when forward declaring.
        if self.forward_decl:
          self.virtual.OnMethod(self.current_class_name, o.name())
          return

        virtual = ''
        if self.decl:
          self.local_var_list = []  # Make a new instance to collect from
          self.local_vars[o] = self.local_var_list

          #log('Is Virtual? %s %s', self.current_class_name, o.name())
          if self.virtual.IsVirtual(self.current_class_name, o.name()):
            virtual = 'virtual '

        if not self.decl and self.current_class_name:
          # definition looks like
          # void Type::foo(...);
          func_name = '%s::%s' % (self.current_class_name, o.name())
        else:
          # declaration inside class { }
          func_name = o.name()

        self.write('\n')

        # TODO: if self.current_class_name ==
        # write 'virtual' here.
        # You could also test NotImplementedError as abstract?

        c_type = get_c_type(o.type.ret_type)
        self.decl_write_ind('%s%s %s(', virtual, c_type, func_name)

        self._write_func_args(o)

        if self.decl:
          self.decl_write(');\n')
          self.in_func_body = True
          self.accept(o.body)  # Collect member_vars, but don't write anything
          self.in_func_body = False
          return

        self.write(') ')

        # Write local vars we collected in the 'decl' phase
        if not self.forward_decl and not self.decl:
          arg_names = [arg.variable.name() for arg in o.arguments]
          no_args = [
              (lval_name, c_type) for (lval_name, c_type) in self.local_vars[o]
              if lval_name not in arg_names
          ]

          self.prepend_to_block = no_args

        self.in_func_body = True
        self.accept(o.body)
        self.in_func_body = False

    def visit_overloaded_func_def(self, o: 'mypy.nodes.OverloadedFuncDef') -> T:
        pass

    def visit_class_def(self, o: 'mypy.nodes.ClassDef') -> T:
        #log('  CLASS %s', o.name)

        base_class_name = None  # single inheritance only
        for b in o.base_type_exprs:
          if isinstance(b, NameExpr):
            # TODO: inherit from std::exception?
            if b.name != 'object' and b.name != 'Exception':
              base_class_name = b.name

        # Forward declare types because they may be used in prototypes
        if self.forward_decl:
          self.decl_write_ind('class %s;\n', o.name)
          if base_class_name:
            self.virtual.OnSubclass(base_class_name, o.name)
          # Visit class body so we get method declarations
          self.current_class_name = o.name
          for stmt in o.defs.body:
            # Ignore things that look like docstrings
            if (isinstance(stmt, ExpressionStmt) and
                isinstance(stmt.expr, StrExpr)):
              continue

            self.accept(stmt)
          self.current_class_name = None
          return

        if self.decl:
          self.member_vars.clear()  # make a new list

          self.decl_write('\n')
          self.decl_write_ind('class %s', o.name)  # block after this

          # e.g. class TextOutput : public ColorOutput
          if base_class_name:
            self.decl_write(' : public %s', base_class_name)

          self.decl_write(' {\n')
          self.decl_write_ind(' public:\n')

          # NOTE: declaration still has to traverse the whole body to fill out
          # self.member_vars!!!
          block = o.defs

          self.indent += 1
          self.current_class_name = o.name
          for stmt in block.body:

            # Ignore things that look like docstrings
            if (isinstance(stmt, ExpressionStmt) and
                isinstance(stmt.expr, StrExpr)):
              continue

            # Constructor is named after class
            if isinstance(stmt, FuncDef) and stmt.name() == '__init__':
              self.decl_write_ind('%s(', o.name)
              self._write_func_args(stmt)
              self.decl_write(');\n')

              # Must visit these for member vars!
              self.accept(stmt.body)
              continue

            self.accept(stmt)

          self.current_class_name = None

          # Now write member defs
          #log('MEMBERS for %s: %s', o.name, list(self.member_vars.keys()))
          if self.member_vars:
            self.decl_write('\n')  # separate from functions
          for name in sorted(self.member_vars):
            c_type = get_c_type(self.member_vars[name])
            self.decl_write_ind('%s %s;\n', c_type, name)

          self.indent -= 1
          self.decl_write_ind('};\n')

          return

        self.current_class_name = o.name

        # Now we're visiting for definitions (not declarations).
        #
        block = o.defs
        for stmt in block.body:

          # Collect __init__ calls within __init__, and turn them into
          # initialize lists.
          if isinstance(stmt, FuncDef) and stmt.name() == '__init__':
            self.write('\n')
            self.write_ind('%s::%s(', o.name, o.name)
            self._write_func_args(stmt)
            self.write(') ')

            # Taking into account the docstring, look at the first statement to
            # see if it's a superclass __init__ call.  Then move that to the
            # initializer list.

            first_index = 0
            maybe_skip_stmt = stmt.body.body[0]
            if (isinstance(maybe_skip_stmt, ExpressionStmt) and
                isinstance(maybe_skip_stmt.expr, StrExpr)):
              first_index += 1

            first_stmt = stmt.body.body[first_index]
            if (isinstance(first_stmt, ExpressionStmt) and
                isinstance(first_stmt.expr, CallExpr)):
              expr = first_stmt.expr
              #log('expr %s', expr)
              callee = first_stmt.expr.callee
              # TextOutput() : ColorOutput(f), ... {
              if isinstance(callee, MemberExpr) and callee.name == '__init__':
                base_constructor_args = expr.args
                #log('ARGS %s', base_constructor_args)
                self.write(': %s(', base_class_name)
                for i, arg in enumerate(base_constructor_args):
                  if i == 0:
                    continue  # Skip 'this'
                  if i != 1:
                    self.write(', ')
                  self.accept(arg)
                self.write(') {\n')

                self.indent += 1
                for node in stmt.body.body[first_index+1:]:
                  self.accept(node)
                self.indent -= 1
                self.write('}\n')
                continue

            # Normal function body
            self.accept(stmt.body)
            continue

          # Write body
          if isinstance(stmt, FuncDef):
            self.accept(stmt)

        self.current_class_name = None   # Stop prefixing functions with class

    def visit_global_decl(self, o: 'mypy.nodes.GlobalDecl') -> T:
        pass

    def visit_nonlocal_decl(self, o: 'mypy.nodes.NonlocalDecl') -> T:
        pass

    def visit_decorator(self, o: 'mypy.nodes.Decorator') -> T:
        pass

    def visit_var(self, o: 'mypy.nodes.Var') -> T:
        pass

    # Module structure

    def visit_import(self, o: 'mypy.nodes.Import') -> T:
        pass

    def visit_import_from(self, o: 'mypy.nodes.ImportFrom') -> T:
        if self.decl:  # No duplicate 'using'
          return

        if o.id in ('__future__', 'typing'):
          return  # do nothing
        if o.names == [('log', None)]:
          return  # do nothing
        if o.names == [('p_die', None)]:
          return  # do nothing

        # Later we need to turn module.func() into module::func(), without
        # disturbing self.foo.
        for name, alias in o.names:
          if alias:
            self.imported_names.add(alias)
          else:
            self.imported_names.add(name)

        # A heuristic that works for the OSH import style.
        #
        # from core.util import log => using core::util::log
        # from core import util => NOT translated

        for name, alias in o.names:
          if '.' in o.id:
            #   from _devbuild.gen.id_kind_asdl import Id
            # -> using id_kind_asdl::Id.
            mod_name = o.id.split('.')[-1]

            # Tag numbers/namespaces end with _n.  enum types end with _e.
            # TODO: rename special cases
            if name.endswith('_n') or name in (
                'hnode_e', 'source_e', 'assign_op_e'):
              self.write_ind(
                  'namespace %s = %s::%s;\n', name, mod_name, name)
            else:
              self.write_ind('using %s::%s;\n', mod_name, name)
          else:
            #    from asdl import format as fmt
            # -> namespace fmt = format;
            if alias:
              self.write_ind('namespace %s = %s;\n', alias, name)
            # If we're importing a module without an alias, we don't need to do
            # anything.  'namespace cmd_exec' is already defined.

        # Old scheme
        # from testpkg import module1 =>
        # namespace module1 = testpkg.module1;
        # Unfortunately the MyPy AST doesn't have enough info to distinguish
        # imported packages and functions/classes?

    def visit_import_all(self, o: 'mypy.nodes.ImportAll') -> T:
        pass

    # Statements

    def visit_block(self, block: 'mypy.nodes.Block') -> T:
        self.write('{\n')  # not indented to use same line as while/if

        self.indent += 1

        if self.prepend_to_block:
          done = set()
          for lval_name, c_type in self.prepend_to_block:
            if lval_name not in done:
              self.write_ind('%s %s;\n', c_type, lval_name)
              done.add(lval_name)
          self.write('\n')
          self.prepend_to_block = None

        for stmt in block.body:
            # Ignore things that look like docstrings
            if isinstance(stmt, ExpressionStmt) and isinstance(stmt.expr, StrExpr):
                continue

            #log('-- %d', self.indent)
            self.accept(stmt)
        self.indent -= 1
        self.write_ind('}\n')

    def visit_expression_stmt(self, o: 'mypy.nodes.ExpressionStmt') -> T:
        # TODO: Avoid writing docstrings.
        # If it's just a string, then we don't need it.

        self.write_ind('')
        self.accept(o.expr)
        self.write(';\n')

    def visit_operator_assignment_stmt(self, o: 'mypy.nodes.OperatorAssignmentStmt') -> T:
        self.write_ind('')
        self.accept(o.lvalue)
        self.write(' %s= ', o.op)  # + to +=
        self.accept(o.rvalue)
        self.write(';\n')

    def visit_while_stmt(self, o: 'mypy.nodes.WhileStmt') -> T:
        self.write_ind('while (')
        self.accept(o.expr)
        self.write(') ')
        self.accept(o.body)

    def visit_return_stmt(self, o: 'mypy.nodes.ReturnStmt') -> T:
        self.write_ind('return ')
        if o.expr:
          self.accept(o.expr)
        self.write(';\n')

    def visit_assert_stmt(self, o: 'mypy.nodes.AssertStmt') -> T:
        pass

    def visit_if_stmt(self, o: 'mypy.nodes.IfStmt') -> T:
        # Not sure why this wouldn't be true
        assert len(o.expr) == 1, o.expr

        # Omit anything that looks like if __name__ == ...
        cond = o.expr[0]
        if (isinstance(cond, ComparisonExpr) and
            isinstance(cond.operands[0], NameExpr) and 
            cond.operands[0].name == '__name__'):
          return

        # Omit if 0:
        if isinstance(cond, IntExpr) and cond.value == 0:
          return

        # Omit if TYPE_CHECKING blocks.  They contain type expressions that
        # don't type check!
        if isinstance(cond, NameExpr) and cond.name == 'TYPE_CHECKING':
          return
        # mylib.CPP
        if isinstance(cond, MemberExpr) and cond.name == 'CPP':
          # just take the if block
          self.write_ind('// if MYCPP\n')
          self.write_ind('')
          for node in o.body:
            self.accept(node)
          self.write_ind('// endif MYCPP\n')
          return
        # mylib.PYTHON
        if isinstance(cond, MemberExpr) and cond.name == 'PYTHON':
          if o.else_body:
            self.write_ind('// if not PYTHON\n')
            self.write_ind('')
            self.accept(o.else_body)
            self.write_ind('// endif MYCPP\n')
          return

        self.write_ind('if (')
        for e in o.expr:
          self.accept(e)
        self.write(') ')

        for node in o.body:
          self.accept(node)

        if o.else_body:
          self.write_ind('else ')
          self.accept(o.else_body)

    def visit_break_stmt(self, o: 'mypy.nodes.BreakStmt') -> T:
        self.write_ind('break;\n')

    def visit_continue_stmt(self, o: 'mypy.nodes.ContinueStmt') -> T:
        self.write_ind('continue;\n')

    def visit_pass_stmt(self, o: 'mypy.nodes.PassStmt') -> T:
        self.write_ind(';  // pass\n')

    def visit_raise_stmt(self, o: 'mypy.nodes.RaiseStmt') -> T:
        self.write_ind('throw ')
        # it could be raise -> throw ; .  OSH uses that.
        if o.expr:
          self.accept(o.expr)
        self.write(';\n')

    def visit_try_stmt(self, o: 'mypy.nodes.TryStmt') -> T:
        self.write_ind('try ')
        self.accept(o.body)
        for t, v, handler in zip(o.types, o.vars, o.handlers):

          # Heuristic
          if isinstance(t, MemberExpr):
            c_type = '%s::%s*' % (t.expr.name, t.name)
          else:
            c_type = '%s*' % t.name

          if v:
            self.write_ind('catch (%s %s) ', c_type, v.name)
          else:
            self.write_ind('catch (%s) ', c_type)
          self.accept(handler)

        if o.else_body:
          raise AssertionError('try/else not supported')
        if o.finally_body:
          raise AssertionError('try/finally not supported')

    def visit_print_stmt(self, o: 'mypy.nodes.PrintStmt') -> T:
        pass

    def visit_exec_stmt(self, o: 'mypy.nodes.ExecStmt') -> T:
        pass

