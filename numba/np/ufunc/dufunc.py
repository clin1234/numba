import functools

import numpy as np

from numba import jit, typeof
from numba.core import cgutils, types, serialize, sigutils, errors
from numba.core.extending import (is_jitted, overload_attribute,
                                  overload_method, register_jitable,
                                  intrinsic)
from numba.core.typing import npydecl
from numba.core.typing.templates import AbstractTemplate, signature
from numba.cpython.unsafe.tuple import tuple_setitem
from numba.np.ufunc import _internal
from numba.parfors import array_analysis
from numba.np.ufunc import ufuncbuilder
from numba.np import numpy_support
from typing import Callable
from llvmlite import ir


def make_dufunc_kernel(_dufunc):
    from numba.np import npyimpl

    class DUFuncKernel(npyimpl._Kernel):
        """
        npyimpl._Kernel subclass responsible for lowering a DUFunc kernel
        (element-wise function) inside a broadcast loop (which is
        generated by npyimpl.numpy_ufunc_kernel()).
        """
        dufunc = _dufunc

        def __init__(self, context, builder, outer_sig):
            super(DUFuncKernel, self).__init__(context, builder, outer_sig)
            self.inner_sig, self.cres = self.dufunc.find_ewise_function(
                outer_sig.args)

        def generate(self, *args):
            isig = self.inner_sig
            osig = self.outer_sig
            cast_args = [self.cast(val, inty, outty)
                         for val, inty, outty in
                         zip(args, osig.args, isig.args)]
            if self.cres.objectmode:
                func_type = self.context.call_conv.get_function_type(
                    types.pyobject, [types.pyobject] * len(isig.args))
            else:
                func_type = self.context.call_conv.get_function_type(
                    isig.return_type, isig.args)
            module = self.builder.block.function.module
            entry_point = cgutils.get_or_insert_function(
                module, func_type,
                self.cres.fndesc.llvm_func_name)
            entry_point.attributes.add("alwaysinline")

            _, res = self.context.call_conv.call_function(
                self.builder, entry_point, isig.return_type, isig.args,
                cast_args)
            return self.cast(res, isig.return_type, osig.return_type)

    DUFuncKernel.__name__ += _dufunc.ufunc.__name__
    return DUFuncKernel


class DUFuncLowerer(object):
    '''Callable class responsible for lowering calls to a specific DUFunc.
    '''
    def __init__(self, dufunc):
        self.kernel = make_dufunc_kernel(dufunc)
        self.libs = []

    def __call__(self, context, builder, sig, args):
        from numba.np import npyimpl
        return npyimpl.numpy_ufunc_kernel(context, builder, sig, args,
                                          self.kernel.dufunc.ufunc,
                                          self.kernel)


class DUFunc(serialize.ReduceMixin, _internal._DUFunc):
    """
    Dynamic universal function (DUFunc) intended to act like a normal
    Numpy ufunc, but capable of call-time (just-in-time) compilation
    of fast loops specialized to inputs.
    """
    # NOTE: __base_kwargs must be kept in synch with the kwlist in
    # _internal.c:dufunc_init()
    __base_kwargs = set(('identity', '_keepalive', 'nin', 'nout'))

    def __init__(self, py_func, identity=None, cache=False, targetoptions={}):
        if is_jitted(py_func):
            py_func = py_func.py_func
        with ufuncbuilder._suppress_deprecation_warning_nopython_not_supplied():
            dispatcher = jit(_target='npyufunc',
                             cache=cache,
                             **targetoptions)(py_func)
        self._initialize(dispatcher, identity)
        functools.update_wrapper(self, py_func)

    def _initialize(self, dispatcher, identity):
        identity = ufuncbuilder.parse_identity(identity)
        super(DUFunc, self).__init__(dispatcher, identity=identity)
        # Loop over a copy of the keys instead of the keys themselves,
        # since we're changing the dictionary while looping.
        self._install_type()
        self._lower_me = DUFuncLowerer(self)
        self._install_cg()
        self.__name__ = dispatcher.py_func.__name__
        self.__doc__ = dispatcher.py_func.__doc__

    def _reduce_states(self):
        """
        NOTE: part of ReduceMixin protocol
        """
        siglist = list(self._dispatcher.overloads.keys())
        return dict(
            dispatcher=self._dispatcher,
            identity=self.identity,
            frozen=self._frozen,
            siglist=siglist,
        )

    @classmethod
    def _rebuild(cls, dispatcher, identity, frozen, siglist):
        """
        NOTE: part of ReduceMixin protocol
        """
        self = _internal._DUFunc.__new__(cls)
        self._initialize(dispatcher, identity)
        # Re-add signatures
        for sig in siglist:
            self.add(sig)
        if frozen:
            self.disable_compile()
        return self

    def build_ufunc(self):
        """
        For compatibility with the various *UFuncBuilder classes.
        """
        return self

    @property
    def targetoptions(self):
        return self._dispatcher.targetoptions

    @property
    def nin(self):
        return self.ufunc.nin

    @property
    def nout(self):
        return self.ufunc.nout

    @property
    def nargs(self):
        return self.ufunc.nargs

    @property
    def ntypes(self):
        return self.ufunc.ntypes

    @property
    def types(self):
        return self.ufunc.types

    @property
    def identity(self):
        return self.ufunc.identity

    @property
    def signature(self):
        return self.ufunc.signature

    def disable_compile(self):
        """
        Disable the compilation of new signatures at call time.
        """
        # If disabling compilation then there must be at least one signature
        assert len(self._dispatcher.overloads) > 0
        self._frozen = True

    def add(self, sig):
        """
        Compile the DUFunc for the given signature.
        """
        args, return_type = sigutils.normalize_signature(sig)
        return self._compile_for_argtys(args, return_type)

    def _compile_for_args(self, *args, **kws):
        nin = self.ufunc.nin
        if kws:
            if 'out' in kws:
                out = kws.pop('out')
                args += (out,)
            if kws:
                raise TypeError("unexpected keyword arguments to ufunc: %s"
                                % ", ".join(repr(k) for k in sorted(kws)))

        args_len = len(args)
        assert (args_len == nin) or (args_len == nin + self.ufunc.nout)
        assert not kws
        argtys = []
        for arg in args[:nin]:
            argty = typeof(arg)
            if isinstance(argty, types.Array):
                argty = argty.dtype
            else:
                # To avoid a mismatch in how Numba types scalar values as
                # opposed to Numpy, we need special logic for scalars.
                # For example, on 64-bit systems, numba.typeof(3) => int32, but
                # np.array(3).dtype => int64.

                # Note: this will not handle numpy "duckarrays" correctly,
                # including but not limited to those implementing `__array__`
                # and `__array_ufunc__`.
                argty = numpy_support.map_arrayscalar_type(arg)
            argtys.append(argty)
        return self._compile_for_argtys(tuple(argtys))

    def _compile_for_argtys(self, argtys, return_type=None):
        """
        Given a tuple of argument types (these should be the array
        dtypes, and not the array types themselves), compile the
        element-wise function for those inputs, generate a UFunc loop
        wrapper, and register the loop with the Numpy ufunc object for
        this DUFunc.
        """
        if self._frozen:
            raise RuntimeError("compilation disabled for %s" % (self,))
        assert isinstance(argtys, tuple)
        if return_type is None:
            sig = argtys
        else:
            sig = return_type(*argtys)
        cres, argtys, return_type = ufuncbuilder._compile_element_wise_function(
            self._dispatcher, self.targetoptions, sig)
        actual_sig = ufuncbuilder._finalize_ufunc_signature(
            cres, argtys, return_type)
        dtypenums, ptr, env = ufuncbuilder._build_element_wise_ufunc_wrapper(
            cres, actual_sig)
        self._add_loop(int(ptr), dtypenums)
        self._keepalive.append((ptr, cres.library, env))
        self._lower_me.libs.append(cres.library)
        return cres

    def _install_ufunc_attributes(self, template) -> None:

        def get_attr_fn(attr: str) -> Callable:

            def impl(ufunc):
                val = getattr(ufunc.key[0], attr)
                return lambda ufunc: val
            return impl

        # ntypes/types needs "at" to be a BoundFunction rather than a Function
        # But this fails as it cannot a weak reference to an ufunc due to NumPy
        # not setting the "tp_weaklistoffset" field. See:
        # https://github.com/numpy/numpy/blob/7fc72776b972bfbfdb909e4b15feb0308cf8adba/numpy/core/src/umath/ufunc_object.c#L6968-L6983  # noqa: E501

        at = types.Function(template)
        attributes = ('nin', 'nout', 'nargs', # 'ntypes', # 'types',
                      'identity', 'signature')
        for attr in attributes:
            attr_fn = get_attr_fn(attr)
            overload_attribute(at, attr)(attr_fn)

    def _install_ufunc_methods(self, template) -> None:
        self._install_ufunc_reduce(template)

    def _install_ufunc_reduce(self, template) -> None:
        at = types.Function(template)

        @overload_method(at, 'reduce')
        def ol_reduce(ufunc, array, axis=0, dtype=None, initial=None):
            if not isinstance(array, types.Array):
                msg = 'The first argument "array" must be array-like'
                raise errors.NumbaTypeError(msg)

            axis_int = isinstance(axis, types.Integer)
            axis_int_tuple = isinstance(axis, types.UniTuple) and \
                isinstance(axis.dtype, types.Integer)
            axis_tuple_size = len(axis) if axis_int_tuple else 0

            if self.ufunc.identity is None and not (
                    axis_int or (axis_int_tuple and axis_tuple_size == 1)):
                msg = (f"reduction operation '{self.ufunc.__name__}' is not "
                       "reorderable, so at most one axis may be specified")
                raise errors.NumbaTypeError(msg)

            tup_init = (0,) * (array.ndim)
            tup_init_m1 = (0,) * (array.ndim - 1)
            nb_dtype = array.dtype if cgutils.is_nonelike(dtype) else dtype
            identity = self.identity

            id_none = cgutils.is_nonelike(identity)
            init_none = cgutils.is_nonelike(initial)

            @register_jitable
            def tuple_slice(tup, pos):
                # Same as
                # tup = tup[0 : pos] + tup[pos + 1:]
                s = tup_init_m1
                i = 0
                for j, e in enumerate(tup):
                    if j == pos:
                        continue
                    s = tuple_setitem(s, i, e)
                    i += 1
                return s

            @register_jitable
            def tuple_slice_append(tup, pos, val):
                # Same as
                # tup = tup[0 : pos] + val + tup[pos + 1:]
                s = tup_init
                i, j, sz = 0, 0, len(s)
                while j < sz:
                    if j == pos:
                        s = tuple_setitem(s, j, val)
                    else:
                        e = tup[i]
                        s = tuple_setitem(s, j, e)
                        i += 1
                    j += 1
                return s

            @intrinsic
            def compute_flat_idx__(typingctx, strides, itemsize, idx, axis):
                sig = types.intp(strides, itemsize, idx, axis)
                len_idx = len(idx)

                def gen_block(builder, block_pos, block_name, bb_end, args):
                    strides, _, idx, _ = args
                    bb = builder.append_basic_block(name=block_name)

                    with builder.goto_block(bb):
                        zero = ir.IntType(64)(0)
                        flat_idx = zero

                        if block_pos == 0:
                            for i in range(1, len_idx):
                                stride = builder.extract_value(strides, i - 1)
                                idx_i = builder.extract_value(idx, i)
                                m = builder.mul(stride, idx_i)
                                flat_idx = builder.add(flat_idx, m)
                        elif 0 < block_pos < len_idx - 1:
                            for i in range(0, block_pos):
                                stride = builder.extract_value(strides, i)
                                idx_i = builder.extract_value(idx, i)
                                m = builder.mul(stride, idx_i)
                                flat_idx = builder.add(flat_idx, m)

                            for i in range(block_pos + 1, len_idx):
                                stride = builder.extract_value(strides, i - 1)
                                idx_i = builder.extract_value(idx, i)
                                m = builder.mul(stride, idx_i)
                                flat_idx = builder.add(flat_idx, m)
                        else:
                            for i in range(0, len_idx - 1):
                                stride = builder.extract_value(strides, i)
                                idx_i = builder.extract_value(idx, i)
                                m = builder.mul(stride, idx_i)
                                flat_idx = builder.add(flat_idx, m)

                        builder.branch(bb_end)

                    return bb, flat_idx

                def codegen(context, builder, sig, args):
                    strides, itemsize, idx, axis = args

                    bb = builder.basic_block
                    switch_end = builder.append_basic_block(name='axis_end')
                    l = []
                    for i in range(len_idx):
                        block, flat_idx = gen_block(builder, i, f"axis_{i}",
                                                    switch_end, args)
                        l.append((block, flat_idx))

                    with builder.goto_block(bb):
                        switch = builder.switch(axis, l[-1][0])
                        for i in range(len_idx):
                            switch.add_case(i, l[i][0])

                    builder.position_at_end(switch_end)
                    phi = builder.phi(l[0][1].type)
                    for block, value in l:
                        phi.add_incoming(value, block)
                    return builder.sdiv(phi, itemsize)

                return sig, codegen

            @register_jitable
            def compute_flat_idx(strides, itemsize, idx, axis):
                flat_idx, i, j, len_idx = 0, 0, 0, len(idx)
                while i < len_idx:
                    if i != axis:
                        flat_idx += strides[j] * idx[i]
                        j += 1
                    i += 1
                flat_idx //= itemsize
                return flat_idx

            @register_jitable
            def find_min(tup):
                idx, e = 0, tup[0]
                for i in range(len(tup)):
                    if tup[i] < e:
                        idx, e = i, tup[i]
                return idx, e

            def impl_1d(ufunc, array, axis=0, dtype=None, initial=None):
                if init_none and id_none:
                    r = array[0]
                elif init_none:
                    r = identity
                else:
                    r = initial

                sz = array.shape[0]
                # XXX: if we have an identity, then this loop starts at 0
                # if not, it should start at 1
                for i in range(sz):
                    r = ufunc(r, array[i])
                return r

            def impl_nd_axis_int(ufunc,
                                 array,
                                 axis=0,
                                 dtype=None,
                                 initial=None):
                if axis is None:
                    raise ValueError("'axis' must be specified")

                if axis < 0 or axis >= array.ndim:
                    raise ValueError("Invalid axis")

                # create result array
                shape = tuple_slice(array.shape, axis)

                if initial is None and identity is None:
                    r = np.empty(shape, dtype=nb_dtype)
                    for idx, _ in np.ndenumerate(r):
                        # shape[0:axis] + 0 + shape[axis:]
                        result_idx = tuple_slice_append(idx, axis, 0)
                        r[idx] = array[result_idx]
                elif initial is None and identity is not None:
                    # Checking if identity is not none is redundant but required
                    # compile this block
                    r = np.full(shape, fill_value=identity, dtype=nb_dtype)
                else:
                    r = np.full(shape, fill_value=initial, dtype=nb_dtype)

                # One approach to implement reduce is to remove the axis index
                # from the indexing tuple returned by "np.ndenumerate". For
                # instance, if idx = (X, Y, Z) and axis=1, the result index
                # is (X, Y).
                # Another way is to compute the result index using strides,
                # which is faster than manipulating tuples.
                view = r.ravel()
                for idx, val in np.ndenumerate(array):
                    flat_pos = compute_flat_idx(r.strides, r.itemsize, idx,
                                                axis)
                    lhs, rhs = view[flat_pos], val
                    view[flat_pos] = ufunc(lhs, rhs)
                return r

            def impl_nd_axis_tuple(ufunc,
                                   array,
                                   axis=0,
                                   dtype=None,
                                   initial=None):
                min_idx, min_elem = find_min(axis)
                r = ufunc.reduce(array,
                                 axis=min_elem,
                                 dtype=dtype,
                                 initial=initial)
                if len(axis) == 1:
                    return r
                elif len(axis) == 2:
                    return ufunc.reduce(r, axis=axis[(min_idx + 1) % 2] - 1)
                else:
                    ax = axis_tup
                    for i in range(len(ax)):
                        if i != min_idx:
                            ax = tuple_setitem(ax, i, axis[i])
                    return ufunc.reduce(r, axis=ax)

            if array.ndim == 1:
                return impl_1d
            else:
                if axis_int_tuple:
                    # axis is tuple of integers
                    axis_tup = (0,) * (len(axis) - 1)
                    return impl_nd_axis_tuple
                elif axis == 0 or isinstance(axis, (types.Integer,
                                                    types.Omitted,
                                                    types.IntegerLiteral)):
                    # axis is default value (0) or an integer
                    return impl_nd_axis_int

    def _install_type(self, typingctx=None):
        """Constructs and installs a typing class for a DUFunc object in the
        input typing context.  If no typing context is given, then
        _install_type() installs into the typing context of the
        dispatcher object (should be same default context used by
        jit() and njit()).
        """
        if typingctx is None:
            typingctx = self._dispatcher.targetdescr.typing_context
        _ty_cls = type('DUFuncTyping_' + self.ufunc.__name__,
                       (AbstractTemplate,),
                       dict(key=self, generic=self._type_me))
        typingctx.insert_user_function(self, _ty_cls)
        self._install_ufunc_attributes(_ty_cls)
        self._install_ufunc_methods(_ty_cls)

    def find_ewise_function(self, ewise_types):
        """
        Given a tuple of element-wise argument types, find a matching
        signature in the dispatcher.

        Return a 2-tuple containing the matching signature, and
        compilation result.  Will return two None's if no matching
        signature was found.
        """
        if self._frozen:
            # If we cannot compile, coerce to the best matching loop
            loop = numpy_support.ufunc_find_matching_loop(self, ewise_types)
            if loop is None:
                return None, None
            ewise_types = tuple(loop.inputs + loop.outputs)[:len(ewise_types)]
        for sig, cres in self._dispatcher.overloads.items():
            if sig.args == ewise_types:
                return sig, cres
        return None, None

    def _type_me(self, argtys, kwtys):
        """
        Implement AbstractTemplate.generic() for the typing class
        built by DUFunc._install_type().

        Return the call-site signature after either validating the
        element-wise signature or compiling for it.
        """
        assert not kwtys
        ufunc = self.ufunc
        _handle_inputs_result = npydecl.Numpy_rules_ufunc._handle_inputs(
            ufunc, argtys, kwtys)
        base_types, explicit_outputs, ndims, layout = _handle_inputs_result
        explicit_output_count = len(explicit_outputs)
        if explicit_output_count > 0:
            ewise_types = tuple(base_types[:-len(explicit_outputs)])
        else:
            ewise_types = tuple(base_types)
        sig, cres = self.find_ewise_function(ewise_types)
        if sig is None:
            # Matching element-wise signature was not found; must
            # compile.
            if self._frozen:
                raise TypeError("cannot call %s with types %s"
                                % (self, argtys))
            self._compile_for_argtys(ewise_types)
            sig, cres = self.find_ewise_function(ewise_types)
            assert sig is not None
        if explicit_output_count > 0:
            outtys = list(explicit_outputs)
        elif ufunc.nout == 1:
            if ndims > 0:
                outtys = [types.Array(sig.return_type, ndims, layout)]
            else:
                outtys = [sig.return_type]
        else:
            raise NotImplementedError("typing gufuncs (nout > 1)")
        outtys.extend(argtys)
        return signature(*outtys)

    def _install_cg(self, targetctx=None):
        """
        Install an implementation function for a DUFunc object in the
        given target context.  If no target context is given, then
        _install_cg() installs into the target context of the
        dispatcher object (should be same default context used by
        jit() and njit()).
        """
        if targetctx is None:
            targetctx = self._dispatcher.targetdescr.target_context
        _any = types.Any
        _arr = types.Array
        # Either all outputs are explicit or none of them are
        sig0 = (_any,) * self.ufunc.nin + (_arr,) * self.ufunc.nout
        sig1 = (_any,) * self.ufunc.nin
        targetctx.insert_func_defn(
            [(self._lower_me, self, sig) for sig in (sig0, sig1)])


array_analysis.MAP_TYPES.append(DUFunc)
