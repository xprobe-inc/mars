# Copyright 1999-2021 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import UserDict
from collections.abc import Iterable
from functools import partial

from .. import opcodes
from ..core import ENTITY_TYPE, ChunkData, Tileable, OutputType
from ..core.custom_log import redirect_custom_log
from ..core.operand import Operand
from ..dataframe.core import DATAFRAME_TYPE, SERIES_TYPE, INDEX_TYPE
from ..serialization.serializables import (
    FunctionField,
    ListField,
    DictField,
    BoolField,
    Int32Field,
)
from ..tensor.core import TENSOR_TYPE
from ..utils import (
    build_fetch_tileable,
    enter_current_session,
    find_objects,
    replace_objects,
    merge_chunks,
    merged_chunk_as_tileable_type,
)
from .operands import RemoteOperandMixin

import numpy as np


class RemoteFunction(RemoteOperandMixin, Operand):
    _op_type_ = opcodes.REMOTE_FUNCATION
    _op_module_ = "remote"

    function = FunctionField("function")
    function_args = ListField("function_args")
    function_kwargs = DictField("function_kwargs")
    retry_when_fail = BoolField("retry_when_fail")
    resolve_tileable_input = BoolField("resolve_tileable_input", default=False)
    n_output = Int32Field("n_output", default=None)

    def __init__(self, output_types=None, **kwargs):
        super().__init__(_output_types=output_types, **kwargs)

    @property
    def output_limit(self):
        return self.n_output or 1

    @property
    def retryable(self) -> bool:
        return self.retry_when_fail

    @classmethod
    def _no_prepare(cls, tileable):
        return isinstance(
            tileable, (TENSOR_TYPE, DATAFRAME_TYPE, SERIES_TYPE, INDEX_TYPE)
        )

    def _set_inputs(self, inputs):
        raw_inputs = getattr(self, "_inputs", None)
        super()._set_inputs(inputs)

        function_inputs = iter(inp for inp in self._inputs)
        mapping = {inp: new_inp for inp, new_inp in zip(inputs, self._inputs)}
        if raw_inputs is not None:
            for raw_inp in raw_inputs:
                if self._no_prepare(raw_inp):
                    if not isinstance(self._inputs[0], ChunkData):
                        # not in tile, set_inputs from tileable
                        mapping[raw_inp] = next(function_inputs)
                    else:
                        # in tile, set_inputs from chunk
                        mapping[raw_inp] = build_fetch_tileable(raw_inp)
                else:
                    mapping[raw_inp] = next(function_inputs)
        self.function_args = replace_objects(self.function_args, mapping)
        self.function_kwargs = replace_objects(self.function_kwargs, mapping)

    def __call__(self):
        find_inputs = partial(find_objects, types=ENTITY_TYPE)
        inputs = find_inputs(self.function_args) + find_inputs(self.function_kwargs)
        if self.n_output is None:
            return self.new_tileable(inputs)
        else:
            return self.new_tileables(
                inputs, kws=[dict(i=i) for i in range(self.n_output)]
            )

    @classmethod
    def tile(cls, op):
        outs = op.outputs
        chunk_op = op.copy().reset_key()

        chunk_inputs = []
        pure_depends = []
        executed = False
        for inp in op.inputs:
            if cls._no_prepare(inp):  # pragma: no cover
                if not executed:
                    # trigger execution
                    yield
                else:
                    executed = True
                # if input is tensor, DataFrame etc,
                # do not prepare data, because the data may be to huge,
                # and users can choose to fetch slice of the data themselves
                pure_depends.extend([not op.resolve_tileable_input] * len(inp.chunks))
            else:
                pure_depends.extend([False] * len(inp.chunks))
            chunk_inputs.extend(inp.chunks)
        chunk_op._pure_depends = pure_depends
        # record tileable op key for chunk op
        chunk_op.tileable_op_key = op.key

        out_chunks = [list() for _ in range(len(outs))]
        chunk_kws = []
        for i, (out, out_type) in enumerate(zip(outs, op.output_types)):
            chunk_params = out.params.copy()
            chunk_params["i"] = i
            chunk_kws.append(chunk_params)
            if out_type == OutputType.dataframe:
                chunk_params["index"] = (0, 0)
                chunk_params["shape"] = (np.nan, np.nan)
            elif out_type == OutputType.series:
                chunk_params["index"] = (0,)
                chunk_params["shape"] = (np.nan,)
            elif out_type == OutputType.df_or_series:
                chunk_params["index"] = (0, 0)
                chunk_params["shape"] = (np.nan, np.nan)
                chunk_params["collapse_axis"] = 1
            else:
                chunk_params["index"] = ()
                chunk_params["shape"] = ()
        chunks = chunk_op.new_chunks(chunk_inputs, kws=chunk_kws)
        for i, c in enumerate(chunks):
            out_chunks[i].append(c)

        kws = []
        for i, (out, out_type) in enumerate(zip(outs, op.output_types)):
            params = out.params.copy()
            params["chunks"] = out_chunks[i]
            if out_type == OutputType.dataframe:
                params["nsplits"] = ((np.nan,), (np.nan,))
            elif out_type == OutputType.series:
                params["nsplits"] = ((np.nan,),)
            else:
                params["nsplits"] = ()
            kws.append(params)
        new_op = op.copy()
        return new_op.new_tileables(op.inputs, kws=kws)

    @classmethod
    @redirect_custom_log
    @enter_current_session
    def execute(cls, ctx, op: "RemoteFunction"):
        class MapperWrapper(UserDict):
            def __getitem__(self, item):
                if op.resolve_tileable_input and isinstance(item, Tileable):
                    index_chunks = [(c.index, ctx[c.key]) for c in item.chunks]
                    merged = merge_chunks(index_chunks)
                    return merged_chunk_as_tileable_type(merged, item)
                return super().__getitem__(item)

        mapping = MapperWrapper(
            {
                inp: ctx[inp.key]
                for inp, is_pure_dep in zip(op.inputs, op.pure_depends)
                if not is_pure_dep
            }
        )

        function = op.function
        function_args = replace_objects(op.function_args, mapping)
        function_kwargs = replace_objects(op.function_kwargs, mapping)

        result = function(*function_args, **function_kwargs)

        if op.n_output is None:
            ctx[op.outputs[0].key] = result
        else:
            if not isinstance(result, Iterable):
                raise TypeError(
                    f"Specifying n_output={op.n_output}, "
                    f"but result is not iterable, got {result}"
                )
            result = list(result)
            if len(result) != op.n_output:
                raise ValueError(
                    f"Length of return value should be {op.n_output}, "
                    f"got {len(result)}"
                )
            for out, r in zip(op.outputs, result):
                ctx[out.key] = r


def spawn(
    func,
    args=(),
    kwargs=None,
    retry_when_fail=False,
    resolve_tileable_input=False,
    n_output=None,
    output_types=None,
    **kw,
):
    """
    Spawn a function and return a Mars Object which can be executed later.

    Parameters
    ----------
    func : function
        Function to spawn.
    args: tuple
       Args to pass to function
    kwargs: dict
       Kwargs to pass to function
    retry_when_fail: bool, default False
       If True, retry when function failed.
    resolve_tileable_input: bool default False
       If True, resolve tileable inputs as values.
    n_output: int
       Count of outputs for the function
    output_types: str or list, default "object"
        Specify type of returned objects.

    Returns
    -------
    Object
        Mars Object.

    Examples
    --------
    >>> import mars.remote as mr
    >>> def inc(x):
    >>>     return x + 1
    >>>
    >>> result = mr.spawn(inc, args=(0,))
    >>> result
    Object <op=RemoteFunction, key=e0b31261d70dd9b1e00da469666d72d9>
    >>> result.execute().fetch()
    1

    List of spawned functions can be converted to :class:`mars.remote.ExecutableTuple`,
    and `.execute()` can be called to run together.

    >>> results = [mr.spawn(inc, args=(i,)) for i in range(10)]
    >>> mr.ExecutableTuple(results).execute().fetch()
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

    Mars Object returned by :meth:`mars.remote.spawn` can be treated
    as arguments for other spawn functions.

    >>> results = [mr.spawn(inc, args=(i,)) for i in range(10)]   # list of spawned functions
    >>> def sum_all(xs):
            return sum(xs)
    >>> mr.spawn(sum_all, args=(results,)).execute().fetch()
    55

    inside a spawned function, new functions can be spawned.

    >>> def driver():
    >>>     results = [mr.spawn(inc, args=(i,)) for i in range(10)]
    >>>     return mr.ExecutableTuple(results).execute().fetch()
    >>>
    >>> mr.spawn(driver).execute().fetch()
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

    Mars tensor, DataFrame and so forth is available in spawned functions as well.

    >>> import mars.tensor as mt
    >>> def driver2():
    >>>     t = mt.random.rand(10, 10)
    >>>     return t.sum().to_numpy()
    >>>
    >>> mr.spawn(driver2).execute().fetch()
    52.47844223908132

    Argument of `n_output` can indicate that the spawned function will return multiple outputs.
    This is important when some of the outputs may be passed to different functions.

    >>> def triage(alist):
    >>>     ret = [], []
    >>>     for i in alist:
    >>>         if i < 0.5:
    >>>             ret[0].append(i)
    >>>         else:
    >>>             ret[1].append(i)
    >>>     return ret
    >>>
    >>> def sum_all(xs):
    >>>     return sum(xs)
    >>>
    >>> l = [0.4, 0.7, 0.2, 0.8]
    >>> la, lb = mr.spawn(triage, args=(l,), n_output=2)
    >>>
    >>> sa = mr.spawn(sum_all, args=(la,))
    >>> sb = mr.spawn(sum_all, args=(lb,))
    >>> mr.ExecutableTuple([sa, sb]).execute().fetch()
    >>> [0.6000000000000001, 1.5]
    """
    if not isinstance(args, tuple):
        args = [args]
    else:
        args = list(args)
    if kwargs is None:
        kwargs = dict()
    if not isinstance(output_types, (list, tuple)):
        if output_types is None:
            output_types = OutputType.object
        elif isinstance(output_types, str):
            output_types = getattr(OutputType, output_types)
        output_types = [output_types] if n_output is None else [output_types] * n_output

    if not isinstance(kwargs, dict):
        raise TypeError("kwargs has to be a dict")

    op = RemoteFunction(
        function=func,
        function_args=args,
        function_kwargs=kwargs,
        retry_when_fail=retry_when_fail,
        resolve_tileable_input=resolve_tileable_input,
        n_output=n_output,
        output_types=output_types,
        **kw,
    )
    if op.extra_params:
        raise ValueError(f"Unexpected kw: {list(op.extra_params)[0]}")
    return op()
