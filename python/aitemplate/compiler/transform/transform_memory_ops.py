#  Copyright (c) Meta Platforms, Inc. and affiliates.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""
Perform memory operator related transformations.
"""
from typing import List

from aitemplate.compiler.tensor_accessor import TensorAccessor

from ...utils import graph_utils
from ..base import Operator, Tensor
from . import transform_utils


def _eliminate_cat(sorted_graph: List[Tensor]) -> List[Tensor]:
    # If we only have a single cat op in the graph, let's keep it.
    # This almost always comes from unit tests.
    if len(graph_utils.get_sorted_ops(sorted_graph)) <= 1:
        return sorted_graph

    single_input_cat_ops = []
    sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
    for op in sorted_ops:
        if op._attrs["op"] != "concatenate":
            continue
        if len(op._attrs["outputs"]) != 1:
            continue
        if len(op._attrs["inputs"]) == 0:
            op._attrs["outputs"][0]._attrs["src_ops"].remove(op)
            op._attrs["outputs"] = []
            continue
        if (len(op._attrs["inputs"]) == 1) and (False not in op._attrs["input_masks"]):
            single_input_cat_ops.append(op)

    for op in single_input_cat_ops:
        transform_utils.remove_single_tensor_op_from_sorted_graph(op)
    return transform_utils.sanitize_sorted_graph(sorted_graph)


def _try_merge_split_cat(first_op: Operator, cat: Operator) -> bool:
    first_op_inputs = first_op._attrs["inputs"]
    first_op_outputs = first_op._attrs["outputs"]
    cat_inputs = cat._attrs["inputs"]
    new_cat_inputs = []
    i = 0
    while i < len(cat_inputs):
        matched = True
        for j, _ in enumerate(first_op_outputs):
            if (i + j >= len(cat_inputs)) or (
                cat_inputs[i + j] is not first_op_outputs[j]
            ):
                matched = False
                break
        if matched:
            new_cat_inputs.extend(first_op._attrs["inputs"])
            i += len(first_op_outputs)
        else:
            new_cat_inputs.append(cat_inputs[i])
            i += 1

    for tensor in new_cat_inputs:
        if tensor in first_op_outputs:
            return False

    cat._attrs["inputs"] = new_cat_inputs
    # make sure all of the input_masks values are True. We may need to
    # change this part later when we have TensorAccessors, depending on
    # the order of the transformations.
    assert all(cat._attrs["input_masks"])
    # make sure input_accessors do not carry any strided information
    assert all(
        accessor.stride_dim is None for accessor in cat._attrs["input_accessors"]
    )
    cat._attrs["input_accessors"] = [TensorAccessor(t) for t in cat._attrs["inputs"]]
    cat._attrs["original_inputs"] = list(new_cat_inputs)
    cat._attrs["input_masks"] = [True] * len(new_cat_inputs)
    for tensor in first_op_inputs:
        tensor._attrs["dst_ops"].remove(first_op)
        tensor._attrs["dst_ops"].add(cat)
    for tensor in first_op_outputs:
        transform_utils.remove_tensor_from_sorted_graph(tensor)
    return True


FIRST_OP_CANDIDATES = {"split", "concatenate"}


def _merge_split_and_cat(sorted_graph: List[Tensor]) -> List[Tensor]:  # noqa: C901
    to_be_merged_ops = []
    visited = set()
    for tensor in sorted_graph:
        src_ops = tensor._attrs["src_ops"]
        if len(src_ops) != 1:
            continue
        src_op = list(src_ops)[0]
        if src_op._attrs["op"] not in FIRST_OP_CANDIDATES:
            continue
        if src_op in visited:
            continue
        first_op = src_op

        cat = None
        found_cat_op = True
        for output_t in first_op._attrs["outputs"]:
            if len(output_t._attrs["dst_ops"]) > 1:
                found_cat_op = False
                break
            next_ops = output_t._attrs["dst_ops"]
            if len(next_ops) != 1:
                break
            next_op = list(next_ops)[0]
            if next_op._attrs["op"] != "concatenate":
                found_cat_op = False
                break
            if cat is None:
                cat = next_op
            if next_op is not cat:
                found_cat_op = False
                break

        if cat is None or not found_cat_op:
            continue

        first_op_dim = (
            first_op._attrs["concat_dim"]
            if first_op._attrs["op"] == "concatenate"
            else first_op._attrs["split_dim"]
        )
        if cat._attrs["concat_dim"] != first_op_dim:
            continue

        to_be_merged_ops.append([first_op, cat])
        visited.add(first_op)
        visited.add(cat)

    for ops in to_be_merged_ops:
        _try_merge_split_cat(ops[0], ops[1])

    return transform_utils.sanitize_sorted_graph(sorted_graph)


def transform_memory_ops(
    sorted_graph: List[Tensor], workdir: str = None
) -> List[Tensor]:
    """
    Eliminates unnecessary cat / split ops.
    """

    funcs = [
        _merge_split_and_cat,
        _eliminate_cat,
    ]
    num_ops = None
    should_continue = True
    while should_continue:
        for func in funcs:
            sorted_graph = func(sorted_graph)
        new_num_ops = len(graph_utils.get_sorted_ops(sorted_graph))
        if num_ops == new_num_ops:
            should_continue = False
        num_ops = new_num_ops
    return sorted_graph
