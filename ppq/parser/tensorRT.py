# This file is created by Nvidia Corp.
# Modified by PPQ develop team.
#
# Copyright 2020 NVIDIA Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import os
from typing import Dict
from typing import List
import struct

import torch
from ppq.core import (DataType, OperationMeta, QuantizationPolicy, NetworkFramework,
                      QuantizationProperty, QuantizationStates, TensorMeta,
                      TensorQuantizationConfig, convert_any_to_torch_tensor,
                      ppq_warning, ppq_info)
from ppq.IR import BaseGraph, GraphExporter
from ppq.IR.quantize import QuantableOperation, QuantableVariable
from ppq.utils.round import ppq_tensor_round

from .caffe_exporter import CaffeExporter
from .onnxruntime_exporter import OnnxExporter, ONNXRUNTIMExporter


class TensorRTExporter_QDQ(ONNXRUNTIMExporter):
    """
    TensorRT PPQ 0.6.4 以来新加入的功能
    
    你需要注意，只有 TensorRT 8.0 以上的版本支持读取 PPQ 导出的量化模型
    并且 TensorRT 对于量化模型的解析存在一些 Bug，
    
    如果你遇到模型解析不对的问题，欢迎随时联系我们进行解决。
    
        已知的问题包括：
        1. 模型导出时最好不要包含其他 opset，如果模型上面带了别的opset，比如 mmdeploy，trt有可能会解析失败
        2. 模型导出时可能出现 Internal Error 10, Invalid Node xxx()，我们还不知道如何解决该问题
    
    Args:
        ONNXRUNTIMExporter (_type_): _description_
    """
    def insert_quant_dequant_on_variable(
        self, graph: BaseGraph, var: QuantableVariable, op: QuantableOperation,
        config: TensorQuantizationConfig) -> None:
        """Insert Quant & Dequant Operation to graph This insertion will
        strictly follows tensorRT format requirement.

        Inserted Quant & Dequant op will just between upstream variable and downstream operation,

        Example 1, Insert quant & dequant between var1 and op1:

        Before insertion:
            var1 --> op1

        After insertion:
            var1 --> quant --> generated_var --> dequant --> generated_var --> op1

        Args:
            graph (BaseGraph): PPQ IR graph.
            var (Variable): upstream variable.
            config (TensorQuantizationConfig, optional): quantization config.
            op (Operation, optional): downstream operation.
        """
        meta = var.meta

        scale  = convert_any_to_torch_tensor(config.scale, dtype=torch.float32)
        offset = ppq_tensor_round(config.offset).type(torch.int8)

        qt_op = graph.create_operation(op_type='QuantizeLinear', attributes={})
        dq_op = graph.create_operation(op_type='DequantizeLinear', attributes={})

        graph.insert_op_between_var_and_op(dq_op, up_var=var, down_op=op)
        graph.insert_op_between_var_and_op(qt_op, up_var=var, down_op=dq_op)

        graph.create_link_with_op(graph.create_variable(value=scale, is_parameter=True), upstream_op=None, downstream_op=qt_op)
        graph.create_link_with_op(graph.create_variable(value=offset, is_parameter=True), upstream_op=None, downstream_op=qt_op)
        graph.create_link_with_op(graph.create_variable(value=scale, is_parameter=True), upstream_op=None, downstream_op=dq_op)
        graph.create_link_with_op(graph.create_variable(value=offset, is_parameter=True), upstream_op=None, downstream_op=dq_op)

        if config.policy.has_property(QuantizationProperty.PER_CHANNEL):
            qt_op.attributes['axis'] = config.channel_axis
            dq_op.attributes['axis'] = config.channel_axis

        # create meta data for qt_op, dq_op
        qt_meta = OperationMeta(
            input_metas    = [TensorMeta(dtype=DataType.FP32, shape=meta.shape),
                              TensorMeta(dtype=DataType.FP32, shape=config.scale.shape),
                              TensorMeta(dtype=DataType.INT8, shape=config.offset.shape)],
            output_metas   = [TensorMeta(dtype=DataType.INT8, shape=meta.shape)],
            operation_name = qt_op.name, operation_type=qt_op.type, executing_order=-1)
        dq_meta = OperationMeta(
            input_metas    = [TensorMeta(dtype=DataType.INT8, shape=meta.shape),
                              TensorMeta(dtype=DataType.FP32, shape=config.scale.shape),
                              TensorMeta(dtype=DataType.INT8, shape=config.offset.shape)],
            output_metas   = [TensorMeta(dtype=DataType.FP32, shape=meta.shape)],
            operation_name = dq_op.name, operation_type=dq_op.type, executing_order=-1)

        qt_op.meta_data = qt_meta
        dq_op.meta_data = dq_meta

    def prepare_graph(self, graph: BaseGraph) -> BaseGraph:
        """TensorRT Demands a custimized QAT model format as it input. With
        this particular format, we only need export input quant config from
        ppq, and only a part of operations is required  to dump its quant
        config.

        Which are:
            _DEFAULT_QUANT_MAP = [_quant_entry(torch.nn, "Conv1d", quant_nn.QuantConv1d),
                      _quant_entry(torch.nn, "Conv2d", quant_nn.QuantConv2d),
                      _quant_entry(torch.nn, "Conv3d", quant_nn.QuantConv3d),
                      _quant_entry(torch.nn, "ConvTranspose1d", quant_nn.QuantConvTranspose1d),
                      _quant_entry(torch.nn, "ConvTranspose2d", quant_nn.QuantConvTranspose2d),
                      _quant_entry(torch.nn, "ConvTranspose3d", quant_nn.QuantConvTranspose3d),
                      _quant_entry(torch.nn, "Linear", quant_nn.QuantLinear),
                      _quant_entry(torch.nn, "LSTM", quant_nn.QuantLSTM),
                      _quant_entry(torch.nn, "LSTMCell", quant_nn.QuantLSTMCell),
                      _quant_entry(torch.nn, "AvgPool1d", quant_nn.QuantAvgPool1d),
                      _quant_entry(torch.nn, "AvgPool2d", quant_nn.QuantAvgPool2d),
                      _quant_entry(torch.nn, "AvgPool3d", quant_nn.QuantAvgPool3d),
                      _quant_entry(torch.nn, "AdaptiveAvgPool1d", quant_nn.QuantAdaptiveAvgPool1d),
                      _quant_entry(torch.nn, "AdaptiveAvgPool2d", quant_nn.QuantAdaptiveAvgPool2d),
                      _quant_entry(torch.nn, "AdaptiveAvgPool3d", quant_nn.QuantAdaptiveAvgPool3d),]

        Reference:
        https://github.com/NVIDIA/TensorRT/blob/main/tools/pytorch-quantization/pytorch_quantization/quant_modules.py

        ATTENTION: MUST USE TENSORRT QUANTIZER TO GENERATE A TENSORRT MODEL.
        """
        self.convert_operation_from_opset11_to_opset13(graph)

        # find all quantable operations:
        for operation in [op for op in graph.operations.values()]:
            if not isinstance(operation, QuantableOperation): continue
            if operation.type in {'Conv', 'Gemm', 'ConvTranspose', 'MatMul'}:
                # for Conv, Gemm, ConvTranspose, TensorRT wants their weight to be quantized,
                # however bias remains fp32.
                assert len(operation.config.input_quantization_config) >= 2, (
                    f'Oops seems operation {operation.name} has less than 2 input.')

                i_config, w_config = operation.config.input_quantization_config[: 2]
                i_var, w_var       = operation.inputs[: 2]

                if QuantizationStates.is_activated(i_config.state):
                    self.insert_quant_dequant_on_variable(graph=graph, var=i_var, config=i_config, op=operation)
                self.insert_quant_dequant_on_variable(graph=graph, var=w_var, config=w_config, op=operation)

            elif operation.type in {'AveragePool', 'GlobalAveragePool'}:
                # for Average pool, tensorRT requires their input quant config.

                assert len(operation.config.input_quantization_config) >= 1, (
                    f'Oops seems operation {operation.name} has less than 1 input.')
                i_config = operation.config.input_quantization_config[0]
                i_var    = operation.inputs[0]

                if QuantizationStates.is_activated(i_config.state):
                    self.insert_quant_dequant_on_variable(graph=graph, var=i_var, config=i_config, op=operation)

            else:
                super().convert_operation(
                    graph=graph, op=operation, 
                    process_activation=True, 
                    process_parameter=True, 
                    quant_param_to_int=False)

        return graph

    def export(self, file_path: str, graph: BaseGraph,
               config_path: str = None, input_shapes: Dict[str, list] = None,
               save_as_external_data: bool = False) -> None:
        # step 1, export onnx file.
        super().export(file_path=file_path, graph=graph, 
                       config_path=None, save_as_external_data=save_as_external_data)

        # step 2, convert onnx file to tensorRT engine.
        try:
            import tensorrt as trt
            TRT_LOGGER = trt.Logger(trt.Logger.INFO)
        except Exception as e:
            raise Exception('TensorRT is not successfully loaded, Exporter can not create tensorRT engine directly, '
                            f'a model named {file_path} has been created so that you can send it to tensorRT manually.')
        network_flags = 1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
        network_flags = network_flags | (1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_PRECISION))

        # step 3, build profile input shape
        # Notice that for each input you should give 3 shapes: (min shape), (opt shape), (max shape)
        if input_shapes is None:
            input_shapes = {input_var.name: [input_var.shape, input_var.shape, input_var.shape]
                            for input_var in graph.inputs.values()}

        with trt.Builder(TRT_LOGGER) as builder, builder.create_network(flags=network_flags) as network, trt.OnnxParser(network, TRT_LOGGER) as parser:

            with open(file_path, 'rb') as model:
                if not parser.parse(model.read()):
                    print ('ERROR: Failed to parse the ONNX file.')
                    for error in range(parser.num_errors):
                        print (parser.get_error(error))
                    return None

            config = builder.create_builder_config()
            config.max_workspace_size = 8 << 30
            config.flags = config.flags | 1 << int(trt.BuilderFlag.INT8)

            profile = builder.create_optimization_profile()

            # build TensorRT Profile
            for idx in range(network.num_inputs):
                inp = network.get_input(idx)

                if inp.is_shape_tensor:
                    if inp.name in input_shapes:
                        shapes = input_shapes[inp.name]
                    else: shapes = None

                    if not shapes:
                        shapes = [(1, ) * inp.shape[0]] * 3
                        print('Setting shape input to {:}. '
                              'If this is incorrect, for shape input: {:}, '
                              'please provide tuples for min, opt, '
                              'and max shapes'.format(shapes[0], inp.name))

                    if not isinstance(shapes, list) or len(shapes) != 3:
                        raise ValueError(f'Profiling shape must be a list with exactly 3 shapes(tuples of int), '
                                         f'while received a {type(shapes)} for input {inp.name}, check your input again.')

                    min, opt, max = shapes
                    profile.set_shape_input(inp.name, min, opt, max)

                elif -1 in inp.shape:
                    if inp.name in input_shapes:
                        shapes = input_shapes[inp.name]
                    else: shapes = None

                    if not shapes:
                        shapes = [(1 if s <= 0 else s for s in inp.shape)] * 3

                    min, opt, max = shapes
                    profile.set_shape(inp.name, min, opt, max)

            config.add_optimization_profile(profile)
            trt_engine = builder.build_engine(network, config)

            # end for

        # end with

        engine_file = file_path.replace('.onnx', '.engine')
        with open(engine_file, 'wb') as file:
            file.write(trt_engine.serialize())


class TensorRTExporter_JSON(GraphExporter):
    def export_quantization_config(self, config_path: str, graph: BaseGraph):
        quant_info = {}
        act_quant_info = {}
        quant_info["act_quant_info"] = act_quant_info
        topo_order =  graph.topological_sort()
        for index, op in enumerate(topo_order):
            
            if op.type in {"Shape", "Gather", "Unsqueeze", "Concat", "Reshape"}:
               continue
            
            if index == 0:
                assert graph.inputs.__contains__(op.inputs[0].name)
                input_cfg = op.config.input_quantization_config[0]
                assert input_cfg.state == QuantizationStates.ACTIVATED and\
                    input_cfg.policy.has_property(QuantizationProperty.PER_TENSOR)
                trt_range_input = input_cfg.scale.item() * (input_cfg.quant_max - input_cfg.quant_min) / 2
                act_quant_info[op.inputs[0].name] = trt_range_input
                output_cfg = op.config.output_quantization_config[0]
                trt_range_output = output_cfg.scale.item() * (output_cfg.quant_max - output_cfg.quant_min) / 2
                act_quant_info[op.outputs[0].name] = trt_range_input

            else:
                if not hasattr(op, 'config'):
                    ppq_warning(f'This op does not write quantization parameters: {op.name}.')
                    continue
                else:
                    ppq_info(f'This op writes quantization parameters: {op.name}')
                    output_cfg = op.config.output_quantization_config[0]
                    trt_range_output = output_cfg.scale.item() * (output_cfg.quant_max - output_cfg.quant_min) / 2
                    act_quant_info[op.outputs[0].name] = trt_range_output
        json_qparams_str = json.dumps(quant_info, indent=4)
        with open(config_path, "w") as json_file:
            json_file.write(json_qparams_str)

    def export_weights(self, graph: BaseGraph, config_path: str = None):
        topo_order =  graph.topological_sort()
        weights_list = []
        for index, op in enumerate(topo_order):
            if op.type in {"Conv", "Gemm"}:
                weights_list.extend(op.parameters)

        weight_file_path = os.path.join(os.path.dirname(config_path), "quantized.wts")

        f = open(weight_file_path, 'w')
        f.write("{}\n".format(len(weights_list)))

        for param in weights_list:
            weight_name = param.name
            weight_value = param.value.reshape(-1).cpu().numpy()
            f.write("{} {}".format(weight_name, len(weight_value)))
            for value in weight_value:
                f.write(" ")
                f.write(struct.pack(">f", float(value)).hex())
            f.write("\n")
        ppq_info(f'Parameters have been saved to file: {weight_file_path}')


    def export(self, file_path: str, graph: BaseGraph, config_path: str = None, input_shapes: List[List[int]] = [[1, 3, 224, 224]]):
        if config_path is not None:
            self.export_quantization_config(config_path, graph)
        self.export_weights(graph, config_path)
        _, ext = os.path.splitext(file_path)
        if ext == '.onnx':
            exporter = OnnxExporter()
            exporter.export(file_path=file_path, graph=graph, config_path=None)
        elif ext in {'.prototxt', '.caffemodel'}:
            exporter = CaffeExporter()
            exporter.export(file_path=file_path, graph=graph, config_path=None, input_shapes=input_shapes)
        
        # no pre-determined export format, we export according to the
        # original model format
        elif graph._built_from == NetworkFramework.CAFFE:
            exporter = CaffeExporter()
            exporter.export(file_path=file_path, graph=graph, config_path=None, input_shapes=input_shapes)
        elif graph._built_from == NetworkFramework.ONNX:
            exporter = OnnxExporter()
            exporter.export(file_path=file_path, graph=graph, config_path=None)
