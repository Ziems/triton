import copy
import itertools
from typing import Optional, Union

import numpy as np
import pytest
import torch
from numpy.random import RandomState

import triton
import triton.language as tl
from triton.code_gen import TensorWrapper

int_dtypes = ['int8', 'int16', 'int32', 'int64']
uint_dtypes = ['uint8', 'uint16', 'uint32', 'uint64']
float_dtypes = ['float16', 'float32', 'float64']
dtypes = int_dtypes + uint_dtypes + float_dtypes

def numpy_random(shape, dtype_str, rs: Optional[RandomState] = None):
    if isinstance(shape, int):
        shape = (shape, )
    if rs is None:
        rs = RandomState(seed=17)
    dtype = bool if dtype_str == 'bool' else getattr(np, dtype_str)
    if dtype_str == 'bool':
        raise AssertionError('fnork')
        return rs.randint(0, 2, shape, dtype=dtype)
    elif dtype_str in int_dtypes or dtype_str in uint_dtypes:
        iinfo = np.iinfo(getattr(np, dtype_str))
        x = rs.randint(iinfo.min, iinfo.max, shape, dtype=dtype)
        x[x == 0] = 1
        return x
    elif dtype_str in float_dtypes:
        return rs.normal(0, 1, shape).astype(dtype)
    else:
        raise RuntimeError(f'Unknown dtype {dtype_str}')


def numpy_to_triton(x: np.ndarray, device='cuda') -> Union[TensorWrapper, torch.Tensor]:
    t = x.dtype.name
    if t in uint_dtypes:
        signed_type_name = t.lstrip('u')  # e.g. "uint16" -> "int16"
        x_signed = x.astype(getattr(np, signed_type_name))
        return TensorWrapper(torch.tensor(x_signed, device=device), getattr(tl, t))
    else:
        return torch.tensor(x, device=device)


def torch_dtype_name(dtype) -> str:
    if isinstance(dtype, triton.language.dtype):
        return dtype.name
    elif isinstance(dtype, torch.dtype):
        return str(dtype).split('.')[1]  # 'torch.int64' -> 'int64'
    else:
        raise TypeError(f'not a triton or torch dtype: {type(dtype)}')


def triton_to_numpy(x):
    if isinstance(x, TensorWrapper):
        return np.array(x.base.cpu(), dtype=getattr(np, torch_dtype_name(x.dtype)))
    elif isinstance(x, torch.Tensor):
        dtype_str = torch_dtype_name(x.dtype)
        dtype = bool if dtype_str == 'bool' else getattr(np, dtype_str)
        return np.array(x.cpu(), dtype=dtype)
    else:
        raise ValueError(f"Not a triton-compatible tensor: {x}")


def triton_empty_like(x):
    if isinstance(x, TensorWrapper):
        return TensorWrapper(torch.empty_like(x.base), dtype=x.dtype)
    elif isinstance(x, torch.Tensor):
        return torch.empty_like(x)
    else:
        raise ValueError(f"Not a triton-compatible tensor: {x}")



def patch_kernel(template, to_replace):
    kernel = copy.deepcopy(template)
    for key, value in to_replace.items():
        kernel.src = kernel.src.replace(key, value)
    return kernel


@pytest.mark.parametrize("dtype_x", [dtype_x for dtype_x in dtypes])
def test_empty_kernel(dtype_x, device='cuda'):
    SIZE = 128
    @triton.jit
    def kernel(X, SIZE: tl.constexpr):
        pass
    x = numpy_to_triton(numpy_random(SIZE, dtype_str=dtype_x), device=device)
    kernel[(1, )](x, SIZE=SIZE, num_warps=4)


# generic test functions
def _test_unary(dtype_x, expr, numpy_expr=None, device='cuda'):
    SIZE = 128
    # define the kernel / launch-grid
    @triton.jit
    def kernel(Z, X, SIZE: tl.constexpr):
        off = tl.arange(0, SIZE)
        x = tl.load(X + off)  # noqa: F841
        z = GENERATE_TEST_HERE  # noqa: F821
        tl.store(Z + off, z)

    kernel = patch_kernel(kernel, {'GENERATE_TEST_HERE': expr})
    # inputs
    x = numpy_random(SIZE, dtype_str=dtype_x)
    if 'log' in expr:
        x = np.abs(x) + 0.01
    # reference result
    z_ref = eval(expr if numpy_expr is None else numpy_expr)
    # triton result
    x_tri = numpy_to_triton(x, device=device)
    z_tri = numpy_to_triton(np.empty_like(z_ref), device=device)
    kernel[(1, )](z_tri, x_tri, SIZE=SIZE, num_warps=4)
    # compare
    np.testing.assert_allclose(z_ref, triton_to_numpy(z_tri), rtol=0.01)


def _binary_op_dtype_override(a: str, b: str) -> Optional[np.dtype]:
    overrides = {
        ('int8', 'uint8'): np.uint8,
        ('int8', 'uint16'): np.uint16,
        ('int8', 'uint32'): np.uint32,
        ('int8', 'uint64'): np.uint64,
        ('int16', 'uint16'): np.uint16,
        ('int16', 'uint32'): np.uint32,
        ('int16', 'uint64'): np.uint64,
        ('int32', 'uint32'): np.uint32,
        ('int32', 'uint64'): np.uint64,
        ('int64', 'uint64'): np.uint64,
    }
    key = (a, b) if a < b else (b, a)
    return overrides.get(key)


def _test_binary(dtype_x, dtype_y, expr, torch_expr=None, mode_x='real', mode_y='real', device='cuda'):
    SIZE = 128
    # define the kernel / launch-grid
    @triton.jit
    def kernel(Z, X, Y, SIZE: tl.constexpr):
        off = tl.arange(0, SIZE)
        x = tl.load(X + off)  # noqa: F841
        y = tl.load(Y + off)  # noqa: F841
        z = GENERATE_TEST_HERE
        tl.store(Z + off, z)

    kernel = patch_kernel(kernel, {'GENERATE_TEST_HERE': expr})
    # inputs
    rs = RandomState(17)
    x = numpy_random(SIZE, dtype_str=dtype_x, rs=rs)
    y = numpy_random(SIZE, dtype_str=dtype_y, rs=rs)
    if mode_x == 'nan': x[:] = float('nan')
    if mode_y == 'nan': y[:] = float('nan')
    # reference result
    z_ref = eval(expr if torch_expr is None else torch_expr)
    dtype_z = _binary_op_dtype_override(dtype_x, dtype_y)
    if dtype_z is not None:
        z_ref = z_ref.astype(dtype_z)
    # triton result
    x_tri = numpy_to_triton(x, device=device)
    y_tri = numpy_to_triton(y, device=device)
    z_tri = numpy_to_triton(np.empty(SIZE, dtype=z_ref.dtype), device=device)
    kernel[(1, )](z_tri, x_tri, y_tri, SIZE=SIZE, num_warps=4)
    # compare
    np.testing.assert_allclose(z_ref, triton_to_numpy(z_tri), err_msg=expr, rtol=0.01)


def _fake_fmod(x, y):
    """
    Triton % (for both integers and floats) has the same semantics as torch
    fmod, but torch fmod doesn't work on integers until torch 1.8.
    `_fake_fmod` gives the same semantics but works on all versions of torch.
    """
    z = torch.remainder(x, y)
    return torch.where((torch.sign(x) != torch.sign(y)) & (z != 0), z - y, z)


def _mod_operation_ill_conditioned(dtype_x, dtype_y) -> bool:
    # The result of x % y is ill-conditioned if x % y is much smaller than x.
    # pytorch/CUDA has slightly different (probably better) rounding on
    # remainders than stock LLVM. We currently don't expect to match it
    # bit-for-bit.
    return (dtype_x, dtype_y) in [
        ('int32', 'float16'),
        ('int32', 'float32'),
        ('int64', 'float16'),
        ('int64', 'float32'),
        ('int64', 'float64'),
    ]

# ---------------
# test binary ops
# ---------------
@pytest.mark.parametrize("dtype_x, dtype_y, op", [
    (dtype_x, dtype_y, op)
  for op in ['+', '-', '*', '/', '%']
  for dtype_x in dtypes
  for dtype_y in dtypes
])
def test_bin_op(dtype_x, dtype_y, op, device='cuda'):
    expr = f' x {op} y'
    if op == '%' and dtype_x in int_dtypes and dtype_y in int_dtypes:
        # LLVM has 'torch.fmod', not 'torch.remainder' semantics on integer remainders.
        torch_expr = '_fake_fmod(x, y)'
    elif op in ('/', '%') and dtype_x in ('int16', 'float16') and dtype_y in ('int16', 'float16'):
        # Triton promotes 16-bit floating-point / and % to 32-bit because there
        # are no native div or FRem operations on float16. Since we have to
        # convert anyway, we may as well take the accuracy bump.
        torch_expr = f'x.to(torch.float32) {op} y.to(torch.float32)'
    else:
        torch_expr = None
    if op == '%' and _mod_operation_ill_conditioned(dtype_x, dtype_y):
        with pytest.raises(AssertionError, match='Arrays are not almost equal'):
            _test_binary(dtype_x, dtype_y, expr, torch_expr=torch_expr, device=device)
    else:
        _test_binary(dtype_x, dtype_y, expr, torch_expr=torch_expr, device=device)



# ---------------
# test bitwise ops
# ---------------
@pytest.mark.parametrize("dtype_x, dtype_y, expr", [
    (dtype_x, dtype_y, f'x{op}y') \
  for op in ['&', '|', '^'] \
  for dtype_x in dtypes \
  for dtype_y in dtypes
])
def test_bitwise_op(dtype_x, dtype_y, expr, device='cuda'):
    if 'float' in dtype_x + dtype_y:
        with pytest.raises(TypeError):
            _test_binary(dtype_x, dtype_y, expr, device=device)
    elif (dtype_x == 'uint64'  and dtype_y in int_dtypes) or (dtype_x in int_dtypes and dtype_y == 'uint64'):
        # TODO(madeleine): Make sure Triton raises an error. This one is from numpy.
        with pytest.raises(TypeError):
            _test_binary(dtype_x, dtype_y, expr, device=device)
    else:
        _test_binary(dtype_x, dtype_y, expr, device=device)


# ---------------
# test compare ops
# ---------------
ops = ['==', '!=', '>', '<', '>=', '<=']
@pytest.mark.parametrize("dtype_x, dtype_y, expr, mode_x, mode_y", \
# real
[
    (dtype_x, dtype_y, f'x{op}y', 'real', 'real') \
    for op in ops \
    for dtype_x in dtypes \
    for dtype_y in dtypes
] + \
# NaNs
[('float32', 'float32', f'x{op}y', mode_x, mode_y) \
    for op in ops
    for mode_x, mode_y in [('nan' , 'real'),
                           ('real', 'nan'),
                           ('nan' , 'nan')]

])
def test_compare_op(dtype_x, dtype_y, expr, mode_x, mode_y, device='cuda'):
    _test_binary(dtype_x, dtype_y, expr, mode_x=mode_x, mode_y=mode_y, device=device)


# ---------------
# test unary ops
# ---------------
@pytest.mark.parametrize("dtype_x, expr", [
    (dtype_x, f' -x') for dtype_x in dtypes
] + [\
    (dtype_x, f' ~x') for dtype_x in int_dtypes
     ])
def test_unary_op(dtype_x, expr, device='cuda'):
    _test_unary(dtype_x, expr, device=device)

# ----------------
# test math ops
# ----------------
# @pytest.mark.paramterize("expr", [
#     'exp', 'log', 'cos', 'sin'
# ])

@pytest.mark.parametrize("expr", [
    'exp', 'log', 'cos', 'sin'
])
def test_math_op(expr, device='cuda'):
    _test_unary('float32', f'tl.{expr}(x)', f'np.{expr}(x) ', device=device)


# ----------------
# test indexing
# ----------------


def make_ptr_str(name, shape):
    rank = len(shape)
    offsets = []
    stride = 1
    for i in reversed(range(rank)):
        idx = ', '.join([':' if ii == i else 'None' for ii in range(rank)])
        offsets += [f'tl.arange(0, {shape[i]})[{idx}]*{stride}']
        stride *= shape[i]
    return f"{name} + {' + '.join(offsets)}"


@pytest.mark.parametrize("expr, dtype_str", [
    (f'x[{s}]', d)
        for s in ['None, :', ':, None', 'None, :, :', ':, :, None']
        for d in ['int32', 'uint32', 'uint16']
])
def test_index1d(expr, dtype_str, device='cuda'):
    rank_x = expr.count(':')
    rank_y = expr.count(',') + 1
    shape_x = [32 for _ in range(rank_x)]
    shape_z = [32 for _ in range(rank_y)]

    # Triton kernel
    @triton.jit
    def kernel(Z, X, SIZE: tl.constexpr):
        m = tl.arange(0, SIZE)
        n = tl.arange(0, SIZE)
        x = tl.load(X_PTR_EXPR)
        z = GENERATE_TEST_HERE
        tl.store(Z_PTR_EXPR, z)

    to_replace = {
        'X_PTR_EXPR': make_ptr_str('X', shape_x),
        'Z_PTR_EXPR': make_ptr_str('Z', shape_z),
        'GENERATE_TEST_HERE': expr,
    }
    kernel = patch_kernel(kernel, to_replace)

    # torch result
    x = numpy_random(shape_x, dtype_str=dtype_str)
    y = np.zeros(shape_z, dtype=getattr(np, dtype_str))
    z_ref = eval(expr) + y
    # triton result
    z_tri = numpy_to_triton(np.empty_like(z_ref), device=device)
    x_tri = numpy_to_triton(x)
    kernel[(1, )](z_tri, x_tri, num_warps=1, SIZE=shape_x[0])
    # compare
    assert (z_ref == triton_to_numpy(z_tri)).all()


# ---------------
# test tuples
# ---------------


@triton.jit
def fn(a, b):
    return a + b, \
            a - b, \
            a * b


def test_tuples():
    device = 'cuda'

    @triton.jit
    def with_fn(X, Y, A, B, C):
        x = tl.load(X)
        y = tl.load(Y)
        a, b, c = fn(x, y)
        tl.store(A, a)
        tl.store(B, b)
        tl.store(C, c)

    @triton.jit
    def without_fn(X, Y, A, B, C):
        x = tl.load(X)
        y = tl.load(Y)
        a, b, c = x + y, x - y, x * y
        tl.store(A, a)
        tl.store(B, b)
        tl.store(C, c)

    x = torch.tensor([1.3], device=device, dtype=torch.float32)
    y = torch.tensor([1.9], device=device, dtype=torch.float32)
    a_tri = torch.tensor([0], device=device, dtype=torch.float32)
    b_tri = torch.tensor([0], device=device, dtype=torch.float32)
    c_tri = torch.tensor([0], device=device, dtype=torch.float32)
    for kernel in [with_fn, without_fn]:
        kernel[(1, )](x, y, a_tri, b_tri, c_tri, num_warps=1)
        a_ref, b_ref, c_ref = x + y, x - y, x * y
        assert a_tri == a_ref
        assert b_tri == b_ref
        assert c_tri == c_ref


# ---------------
# test atomics
# ---------------
@pytest.mark.parametrize("op, dtype_x_str, mode", itertools.chain.from_iterable([
    [
        ('add', 'float16', mode),
        ('add', 'uint32', mode), ('add', 'int32', mode), ('add', 'float32', mode),
        ('max', 'uint32', mode), ('max', 'int32', mode), ('max', 'float32', mode),
        ('min', 'uint32', mode), ('min', 'int32', mode), ('min', 'float32', mode),
    ]
    for mode in ['all_neg', 'all_pos', 'min_neg', 'max_pos']]))
def test_atomic_rmw(op, dtype_x_str, mode, device='cuda'):
    n_programs = 5

    # triton kernel
    @triton.jit
    def kernel(X, Z):
        pid = tl.program_id(0)
        x = tl.load(X + pid)
        old = GENERATE_TEST_HERE

    kernel = patch_kernel(kernel, {'GENERATE_TEST_HERE': f'tl.atomic_{op}(Z, x)'})
    numpy_op = {'add': np.sum, 'max': np.max, 'min': np.min}[op]
    max_neutral = float('-inf') if dtype_x_str in float_dtypes else np.iinfo(getattr(np, dtype_x_str)).min
    min_neutral = float('inf') if dtype_x_str in float_dtypes else np.iinfo(getattr(np, dtype_x_str)).max
    neutral = {'add': 0, 'max': max_neutral, 'min': min_neutral}[op]

    # triton result
    x = numpy_random((n_programs, ), dtype_str=dtype_x_str)
    if mode == 'all_neg':
        x = -np.abs(x)
    if mode == 'all_pos':
        x = np.abs(x)
    if mode == 'min_neg':
        idx = np.random.randint(n_programs, size=(1, )).item()
        x[idx] = -np.max(np.abs(x)) - 1
    if mode == 'max_pos':
        idx = np.random.randint(n_programs, size=(1, )).item()
        x[idx] = np.max(np.abs(x)) + 1
    x_tri = numpy_to_triton(x, device=device)

    z_tri = numpy_to_triton(np.array([neutral], dtype=getattr(np, dtype_x_str)), device=device)
    kernel[(n_programs, )](x_tri, z_tri)
    # torch result
    z_ref = numpy_op(x).astype(getattr(np, dtype_x_str))
    # compare
    exact = op not in ['add']
    if exact:
        assert z_ref.item() == triton_to_numpy(z_tri).item()
    else:
        np.testing.assert_allclose(z_ref, triton_to_numpy(z_tri), rtol=0.001)


# ---------------
# test cast
# ---------------
@pytest.mark.parametrize("dtype_x, dtype_z, bitcast", [
    (dtype_x, dtype_z, False)
                        for dtype_x in dtypes
                        for dtype_z in dtypes
] + [
    ('float32', 'bfloat16', False),
    ('bfloat16', 'float32', False),
] + [
    (f'uint{x}', f'int{x}', True) for x in [8, 16, 32, 64]
] + [
    (f'int{x}', f'uint{x}', True) for x in [8, 16, 32, 64]
]
)
def test_cast(dtype_x, dtype_z, bitcast, device='cuda'):
    # This is tricky because numpy doesn't have bfloat, and torch doesn't have uints.
    x0 = 43 if dtype_x in int_dtypes else 43.5
    if dtype_x.startswith('bfloat'):
        x_tri = torch.tensor([x0], dtype=getattr(torch, dtype_x), device=device)
    else:
        x = np.array([x0], dtype=getattr(np, dtype_x))
        x_tri = numpy_to_triton(x)

    # triton kernel
    @triton.jit
    def kernel(X, Z, BITCAST: tl.constexpr):
        x = tl.load(X)
        z = x.to(Z.dtype.element_ty, bitcast = BITCAST)
        tl.store(Z, z)

    # triton result
    if dtype_z.startswith('bfloat'):
        z_tri = torch.empty((1,), dtype=getattr(torch, dtype_z), device=device)
    else:
        z_tri = numpy_to_triton(np.empty((1, ), dtype=getattr(np, dtype_z)), device=device)
    kernel[(1, )](x_tri, z_tri, BITCAST=bitcast)
    # torch result
    if dtype_z.startswith('bfloat') or dtype_x.startswith('bfloat'):
        assert bitcast is False
        z_ref = x_tri.to(z_tri.dtype)
        assert z_tri == z_ref
    else:
        if bitcast:
            z_ref = x.view(getattr(np, dtype_z))
        else:
            z_ref = x.astype(getattr(np, dtype_z))
        assert triton_to_numpy(z_tri) == z_ref

# ---------------
# test reduce
# ---------------
@pytest.mark.parametrize("dtype_str, shape",
  [(dtype, shape) \
        for dtype in dtypes\
        for shape in [128, 512]])
def test_reduce1d(dtype_str, shape, device='cuda'):

    # triton kernel
    @triton.jit
    def kernel(X, Z, BLOCK: tl.constexpr):
        x = tl.load(X + tl.arange(0, BLOCK))
        tl.store(Z, tl.sum(x, axis=0))

    rs = RandomState(17)
    x = numpy_random((shape,), dtype_str=dtype_str, rs=rs)
    # numpy result
    z_ref = np.sum(x).astype(getattr(np, dtype_str))
    # triton result
    x_tri = numpy_to_triton(x, device=device)
    z_tri = numpy_to_triton(numpy_random((1,), dtype_str=dtype_str, rs=rs), device=device)
    kernel[(1,)](x_tri, z_tri, BLOCK=shape)
    # compare
    np.testing.assert_allclose(z_ref, triton_to_numpy(z_tri), rtol=0.01)


@pytest.mark.parametrize("dtype_str, shape, axis",
  [(dtype, shape, 1)
        for dtype in ['float32', 'uint32']
        for shape in [(1, 1024)]])
def test_reduce2d(dtype_str, shape, axis, device='cuda'):
    # triton kernel
    @triton.jit
    def kernel(X, Z, BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr, AXIS: tl.constexpr):
        range_m = tl.arange(0, BLOCK_M)
        range_n = tl.arange(0, BLOCK_N)
        x = tl.load(X + range_m[:, None]*BLOCK_N + range_n[None, :])
        z = tl.sum(x, axis=AXIS)
        tl.store(Z + range_m, z)
    # input
    x = numpy_random(shape, dtype_str=dtype_str)
    # triton result
    x_tri = numpy_to_triton(x)
    z_tri = numpy_to_triton(np.empty((shape[0],), dtype=getattr(np, dtype_str)), device=device)
    kernel[(1,)](x_tri, z_tri, BLOCK_M=shape[0], BLOCK_N=shape[1], AXIS=axis)
    # numpy reference result
    z_ref = np.sum(x, axis=axis).astype(x.dtype)
    # compare
    np.testing.assert_allclose(z_ref, triton_to_numpy(z_tri), rtol=0.01)

# ---------------
# test permute
# ---------------

@pytest.mark.parametrize("dtype_str, shape, perm",
  [(dtype, shape, perm) \
        for dtype in ['float32']\
        for shape in [(128, 128)]\
        for perm  in [(1, 0)]])
def test_permute(dtype_str, shape, perm, device='cuda'):

    # triton kernel
    @triton.jit
    def kernel(X, stride_xm, stride_xn,
               Z, stride_zm, stride_zn,
               BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr):
        off_m = tl.arange(0, BLOCK_M)
        off_n = tl.arange(0, BLOCK_N)
        Xs = X + off_m[:, None] * stride_xm + off_n[None, :] * stride_xn
        Zs = Z + off_m[:, None] * stride_zm + off_n[None, :] * stride_zn
        tl.store(Zs, tl.load(Xs))
    # input
    x = numpy_random(shape, dtype_str=dtype_str)
    # triton result
    z_tri = numpy_to_triton(np.empty_like(x), device=device)
    x_tri = numpy_to_triton(x, device=device)
    pgm = kernel[(1, 1)](x_tri, x_tri.stride(0), x_tri.stride(1),
                         z_tri, z_tri.stride(1), z_tri.stride(0),
                         BLOCK_M=shape[0], BLOCK_N=shape[1])
    # torch result
    z_ref = x.transpose(*perm)
    # compare
    triton.testing.assert_almost_equal(z_tri, z_ref)
    # parse ptx to make sure ld/st are vectorized
    ptx = pgm.asm['ptx']
    assert 'ld.global.v4' in ptx
    assert 'st.global.v4' in ptx

# ---------------
# test dot
# ---------------

@pytest.mark.parametrize("epilogue", ['none', 'trans', 'add-matrix', 'add-rows', 'add-cols'])
def test_dot(epilogue, device='cuda'):
    # triton kernel
    @triton.jit
    def kernel(X, stride_xm, stride_xk,
               Y, stride_yk, stride_yn,
               Z, stride_zm, stride_zn,
               BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr, BLOCK_K: tl.constexpr,
               ADD_MATRIX: tl.constexpr, ADD_ROWS: tl.constexpr, ADD_COLS: tl.constexpr):
        off_m = tl.arange(0, BLOCK_M)
        off_n = tl.arange(0, BLOCK_N)
        off_k = tl.arange(0, BLOCK_K)
        Xs = X + off_m[:, None] * stride_xm + off_k[None, :] * stride_xk
        Ys = Y + off_k[:, None] * stride_yk + off_n[None, :] * stride_yn
        Zs = Z + off_m[:, None] * stride_zm + off_n[None, :] * stride_zn
        z = tl.dot(tl.load(Xs), tl.load(Ys))
        if ADD_MATRIX:
            z += tl.load(Zs)
        if ADD_ROWS:
            ZRs = Z + off_m * stride_zm
            z += tl.load(ZRs)[:, None]
        if ADD_COLS:
            ZCs = Z + off_n * stride_zn
            z += tl.load(ZCs)[None, :]
        tl.store(Zs, z)
    # input
    M, N, K = 64, 64, 32
    rs = RandomState(17)
    x = numpy_random((M, K), dtype_str='float32', rs=rs)
    y = numpy_random((K, N), dtype_str='float32', rs=rs)
    x_tri = numpy_to_triton(x, device=device)
    y_tri = numpy_to_triton(y, device=device)
    # triton result
    z = numpy_random((M, N), dtype_str='float32', rs=rs)
    z_tri = numpy_to_triton(z, device=device)
    if epilogue == 'trans':
        z_tri = torch.as_strided(z_tri, (M, N), z_tri.stride()[::-1])
    pgm = kernel[(1, 1)](x_tri, x_tri.stride(0), x_tri.stride(1),
                         y_tri, y_tri.stride(0), y_tri.stride(1),
                         z_tri, z_tri.stride(0), z_tri.stride(1),
                         BLOCK_M=M, BLOCK_K=K, BLOCK_N=N,
                         ADD_MATRIX = epilogue=='add-matrix',
                         ADD_ROWS = epilogue=='add-rows',
                         ADD_COLS = epilogue=='add-cols')
    # torch result
    z_ref = np.matmul(x, y)
    if epilogue == 'add-matrix':
        z_ref += z
    if epilogue == 'add-rows':
        z_ref += z[:,0][:, None]
    if epilogue == 'add-cols':
        z_ref += z[0,:][None, :]
    # compare
    np.testing.assert_allclose(z_ref, triton_to_numpy(z_tri), rtol=0.01)
    # make sure ld/st are vectorized
    ptx = pgm.asm['ptx']
    assert 'ld.global.v4' in ptx
    assert 'st.global.v4' in ptx

def test_dot_without_load():
    @triton.jit
    def kernel(out):
        pid = tl.program_id(axis=0)
        a = tl.zeros((32, 32), tl.float32)
        b = tl.zeros((32, 32), tl.float32)
        c = tl.zeros((32, 32), tl.float32)
        c = tl.dot(a, b)
        pout = out + tl.arange(0, 32)[:, None]*32 + tl.arange(0, 32)[None, :]
        tl.store(pout, c)

    out = torch.ones((32,32), dtype=torch.float32, device="cuda")
    kernel[(1,)](out)

# ---------------
# test arange
# ---------------

@pytest.mark.parametrize("start", [0, 1, 7, 16])
def test_arange(start, device='cuda'):
    BLOCK = 128
    z_tri = torch.empty(BLOCK, dtype=torch.int32, device=device)
    @triton.jit
    def _kernel(z, BLOCK: tl.constexpr,
                START: tl.constexpr, END: tl.constexpr):
        off = tl.arange(0, BLOCK)
        val = tl.arange(START, END)
        tl.store(z + off, val)
    _kernel[(1,)](z_tri, START=start, END=start+BLOCK, BLOCK=BLOCK)
    z_ref = torch.arange(start, BLOCK+start, dtype=torch.int32, device=device)
    triton.testing.assert_almost_equal(z_tri, z_ref)

# ---------------
# test load
# ---------------
# 'bfloat16': torch.bfloat16,
# Testing masked loads with an intermate copy to shared memory run.
@pytest.mark.parametrize("dtype", [torch.float16, torch.float32])
def test_masked_load_shared_memory(dtype, device='cuda'):
    M = 32
    N = 32
    K = 8

    in1 = torch.rand((M, K), dtype=dtype, device=device)
    in2 = torch.rand((K, N), dtype=dtype, device=device)
    out = torch.zeros((M, N), dtype=dtype, device=device)

    @triton.jit
    def _kernel(in1_ptr, in2_ptr, output_ptr,
                in_stride, in2_stride, out_stride,
                in_numel, in2_numel, out_numel,
                M: tl.constexpr, N: tl.constexpr, K: tl.constexpr):

        M_offsets = tl.arange(0, M)
        N_offsets = tl.arange(0, N)
        K_offsets = tl.arange(0, K)

        in_offsets =  M_offsets[:, None] * in_stride + K_offsets[None,:]
        in2_offsets =  K_offsets[:, None] * in2_stride + N_offsets[None,:]

        # Load inputs.
        x = tl.load(in1_ptr + in_offsets, mask=in_offsets < in_numel)
        w = tl.load(in2_ptr + in2_offsets, mask=in2_offsets < in2_numel)

        # Without a dot product the memory doesn't get promoted to shared.
        o = tl.dot(x, w)

        # Store output
        output_offsets =  M_offsets[:, None] * out_stride + N_offsets[None,:]
        tl.store(output_ptr + output_offsets, o, mask=output_offsets < in2_numel)

    pgm = _kernel[(1,)](in1, in2, out,
                  in1.stride()[0],
                  in2.stride()[0],
                  out.stride()[0],
                  in1.numel(),
                  in2.numel(),
                  out.numel(),
                  M=M, N=N, K=K)

    reference_out =torch.matmul(in1, in2)
    triton.testing.allclose(out, reference_out)

@pytest.mark.parametrize("cache", ["", ".ca", ".cg"])
def test_load_cache_modifier(cache):
    src = torch.empty(128, device='cuda')
    dst = torch.empty(128, device='cuda')

    @triton.jit
    def _kernel(dst, src, CACHE: tl.constexpr):
        offsets = tl.arange(0, 128)
        x = tl.load(src+offsets, cache_modifier=CACHE)
        tl.store(dst+offsets, x)

    pgm = _kernel[(1,)](dst, src, CACHE=cache)
    ptx = pgm.asm['ptx']
    if cache == '':
        assert 'ld.global.ca' not in ptx
        assert 'ld.global.cg' not in ptx
    if cache == '.cg':
        assert 'ld.global.cg' in ptx
        assert 'ld.global.ca' not in ptx
    if cache == '.ca':
        assert 'ld.global.ca' in ptx
        assert 'ld.global.cg' not in ptx

# ---------------
# test store
# ---------------

# ---------------
# test if
# ---------------

# ---------------
# test for
# ---------------

# ---------------
# test while
# ---------------

# ---------------
# test default
# ---------------
#TODO: can't be local to test_default
@triton.jit
def _impl(value = 10):
    return value

def test_default():
    value = 5
    ret0 = torch.zeros(1, dtype=torch.int32, device='cuda')
    ret1 = torch.zeros(1, dtype=torch.int32, device='cuda')

    @triton.jit
    def _kernel(ret0, ret1, value):
        tl.store(ret0, _impl())
        tl.store(ret1, _impl(value))

    _kernel[(1,)](ret0, ret1, value)
    assert ret0.item() == 10
    assert ret1.item() == value

# ---------------
# test noop
#----------------
def test_noop(device='cuda'):
    @triton.jit
    def kernel(x):
        pass
    x = numpy_to_triton(numpy_random((1,), dtype_str='int32'), device=device)
    kernel[(1, )](x)
