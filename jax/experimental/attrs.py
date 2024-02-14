# Copyright 2024 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from typing import Any

from jax._src import core
from jax._src import api_util
from jax._src import linear_util as lu
from jax._src.api_util import flatten_fun_nokwargs
from jax._src.interpreters import ad
from jax._src.interpreters import partial_eval as pe
from jax._src.tree_util import tree_flatten, tree_unflatten
from jax._src.util import unzip2

JaxVal = Any

register = api_util.register_class_with_attrs

class GetAttrPrimitive(core.Primitive):
  def bind_with_trace(self, trace, args, params):
    () = args
    return trace.process_getattr(**params)
getattr_p = GetAttrPrimitive('getattr')

class SetAttrPrimitive(core.Primitive):
  def bind_with_trace(self, trace, args, params):
    val, = args
    return trace.process_setattr(trace.full_raise(val), **params)
setattr_p = SetAttrPrimitive('setattr')

def jax_getattr(obj: Any, attr: str):
  return getattr_p.bind(obj=obj, attr=attr)

def jax_setattr(obj: Any, attr: str, val: JaxVal):
  setattr_p.bind(val, obj=obj, attr=attr)


def _getattr_impl(_, *, obj, attr):
  return getattr(obj, attr)
core.EvalTrace.process_getattr = _getattr_impl

def _setattr_impl(_, val, *, obj, attr):
  setattr(obj, attr, val)
core.EvalTrace.process_setattr = _setattr_impl


def _ensure_tracked(trace: pe.DynamicJaxprTrace, obj: Any, attr: str):
  frame = trace.main.jaxpr_stack[-1]  # type: ignore
  if (obj, attr) not in frame.attrs_tracked:
    init_val = getattr(obj, attr)
    aval = core.raise_to_shaped(core.get_aval(init_val))
    tracer = pe.DynamicJaxprTracer(trace, aval, pe.source_info_util.current())
    var = frame.tracer_to_var[id(tracer)] = frame.newvar(aval)
    setattr(obj, attr, tracer)
    frame.attrs_tracked.append((obj, attr))
    frame.attrs_inits.append(init_val)
    frame.attrs_vars.append(var)
pe.DynamicJaxprTrace._ensure_tracked = _ensure_tracked

def _getattr_staging(trace, *, obj, attr):
  trace._ensure_tracked(obj, attr)
  return getattr(obj, attr)
pe.DynamicJaxprTrace.process_getattr = _getattr_staging

def _setattr_staging(trace, tracer, *, obj, attr):
  trace._ensure_tracked(obj, attr)
  setattr(obj, attr, tracer)
pe.DynamicJaxprTrace.process_setattr = _setattr_staging


def jvp(f, primals, tangents, tangent_attrs_in):
  primals_flat, in_tree = tree_flatten(primals)
  tangents_flat, in_tree_ = tree_flatten(tangents)
  if in_tree != in_tree_: raise Exception
  f_, out_tree = flatten_fun_nokwargs(lu.wrap_init(f), in_tree)
  out_primals_flat, out_tangents_flat, tangent_attrs_out = _jvp(f_).call_wrapped(
      primals_flat, tangents_flat, tangent_attrs_in)
  out_primals = tree_unflatten(out_tree(), out_primals_flat)
  out_tangents = tree_unflatten(out_tree(), out_tangents_flat)
  return out_primals, out_tangents, tangent_attrs_out

def _jvp(fun: lu.WrappedFun):
  return jvpfun2(jvp_subtrace2(fun))

@lu.transformation
def jvpfun2(primals, tangents, tangent_attrs_in):
  with core.new_main(ad.JVPTrace) as main:
    out_primals, out_tangents, tangent_attrs_out = \
        yield (main, primals, tangents, tangent_attrs_in), {}
    del main
  yield out_primals, out_tangents, tangent_attrs_out

@lu.transformation
def jvp_subtrace2(main, primals, tangents, tangent_attrs_in):
  main.attrs_tracked = []  # attrs written to
  trace = main.with_cur_sublevel()
  for obj, name, tangent in tangent_attrs_in:
    primal = jax_getattr(obj, name)
    tracer = ad.JVPTracer(trace, primal, tangent)
    jax_setattr(obj, name, tracer)
  in_tracers = [ad.JVPTracer(trace, x, t) if type(t) is not ad.Zero else x
                for x, t in zip(primals, tangents)]
  ans = yield in_tracers, {}
  out_tracers = map(trace.full_raise, ans)
  out_primals, out_tangents = unzip2((t.primal, t.tangent) for t in out_tracers)
  tangent_attrs_out = []
  for (obj, name) in main.attrs_tracked:
    tracer = trace.full_raise(jax_getattr(obj, name))
    jax_setattr(obj, name, tracer.primal)
    if type(tracer.tangent) is not ad.Zero:
      tangent_attrs_out.append((obj, name, tracer.tangent))
  del main.attrs_tracked
  yield out_primals, out_tangents, tangent_attrs_out

def _setattr_jvp(trace, tracer, *, obj, attr):
  if (obj, attr) not in trace.main.attrs_tracked:
    trace.main.attrs_tracked.append((obj, attr))
  setattr(obj, attr, tracer)
ad.JVPTrace.process_setattr = _setattr_jvp
