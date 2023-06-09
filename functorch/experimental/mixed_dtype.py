from functools import partial 

import torch
import torch.utils._pytree as pytree
from torch.fx.experimental.proxy_tensor import (
    ProxyTorchDispatchMode,
    disable_proxy_modes_tracing,
    ProxyTorchDispatchMode,
    track_tensor_tree,
    unwrap_proxy,
)
from torch.utils._python_dispatch import (
    _get_current_dispatch_mode,
    _pop_mode_temporarily,
)
from torch._C import DispatchKey
from torch._functorch.eager_transforms import (
    _unwrap_all_tensors_from_functional,
    _wrap_all_tensors_to_functional,
)
from torch._ops import HigherOrderOperator
from torch._subclasses.fake_tensor import FakeTensorMode


mixed_dtype = HigherOrderOperator("mixed_dtype")
mixed_dtype.fallthrough(DispatchKey.PythonDispatcher)
mixed_dtype.fallthrough(DispatchKey.PythonTLSSnapshot)
mixed_dtype.fallthrough(DispatchKey.ADInplaceOrView)
mixed_dtype.fallthrough(DispatchKey.BackendSelect)
mixed_dtype.fallthrough(DispatchKey.AutocastCPU)


def trace_mixed_dtype(proxy_mode, func_overload, op, out_dtype, *args):
    if not isinstance(op, torch._ops.OpOverload):
        raise ValueError("mixed_dtype's first argument must be an OpOverload")

    with disable_proxy_modes_tracing():
        casted_args = pytree.tree_map_only(
            torch.Tensor, lambda arg: arg.to(dtype=out_dtype), args
        )
        out = op(*casted_args)

    node_args = (op, out_dtype, *args)
    proxy_args = pytree.tree_map(partial(unwrap_proxy, proxy_mode), node_args)
    out_proxy = proxy_mode.tracer.create_proxy(
        "call_function", func_overload, proxy_args, {}, name="mixed_dtype"
    )
    return track_tensor_tree(out, out_proxy, constant=None, tracer=proxy_mode.tracer)


@mixed_dtype.py_impl(DispatchKey.CompositeExplicitAutograd)
def mixed_dtype_dense(
    op: torch._ops.OpOverload,
    out_dtype: torch.dtype,
    *args
):
    casted_args = pytree.tree_map_only(
        torch.Tensor, lambda arg: arg.to(dtype=out_dtype), args
    )
    res = op(*casted_args)
    return res


@mixed_dtype.py_impl(DispatchKey.Autograd)
def mixed_dtype_autograd(
    op: torch._ops.OpOverload,
    out_dtype: torch.dtype,
    *args
):
    # TODO: support autograd
    flat_operands, _ = pytree.tree_flatten(args)
    assert all(
        [not f.requires_grad for f in flat_operands if isinstance(f, torch.Tensor)]
    )

    _ = torch._C.ExcludeDispatchKeyGuard(
        torch._C.DispatchKeySet(torch._C.DispatchKey.AutogradCPU)
    )
    return mixed_dtype(op, out_dtype, *args)


@mixed_dtype.py_impl(ProxyTorchDispatchMode)
def mixed_dtype_proxy(
    op: torch._ops.OpOverload,
    out_dtype: torch.dtype,
    *args
):
    mode = _get_current_dispatch_mode()
    assert (mode is not None), "Mode should always be enabled for python fallback key"
    with _pop_mode_temporarily() as mode:
        if mode.enable_tracing:
            return trace_mixed_dtype(mode, mixed_dtype, op, out_dtype, *args)
        else:
            return mixed_dtype(op, out_dtype, *args)


@mixed_dtype.py_impl(FakeTensorMode)
def mixed_dtype_fake_tensor_mode(
    op: torch._ops.OpOverload,
    out_dtype: torch.dtype,
    *args
):
    return mixed_dtype_dense(op, out_dtype, *args)


@mixed_dtype.py_impl(torch._C.DispatchKey.Functionalize)
def mixed_dtype_func(op, out_dtype, *args):
    reapply_views = torch._C._functionalization_reapply_views_tls()
    # At this point, we will see functionalized tensors, so need to unwrap them first
    unwrapped_args = tuple(
        _unwrap_all_tensors_from_functional(arg, reapply_views=reapply_views)
        for arg in args
    )
    # pyre-ignore
    guard = torch._C.ExcludeDispatchKeyGuard(
        torch._C.DispatchKeySet(torch._C.DispatchKey.Functionalize)
    )
    try:
        res = mixed_dtype(op, out_dtype, *unwrapped_args)
        return _wrap_all_tensors_to_functional(res, level=0)
    finally:
        del guard


@mixed_dtype.py_impl(torch._C._functorch.TransformType.Functionalize)
def mixed_dtype_func(interpreter, op, out_dtype, *args):
    reapply_views = interpreter.functionalize_add_back_views()
    # At this point, we will see functionalized tensors, so need to unwrap them first
    unwrapped_args = tuple(
        _unwrap_all_tensors_from_functional(arg, reapply_views=reapply_views)
        for arg in args
    )
    
    with interpreter.lower():
        res = mixed_dtype(op, out_dtype, *unwrapped_args)
        return _wrap_all_tensors_to_functional(res, level=interpreter.level())
