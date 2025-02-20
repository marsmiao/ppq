# ---------------------------------------------------------------
# 这个脚本向你展示了如何使用 tensorRT 对 PPQ 导出的模型进行推理

# This script shows you how to export ppq internal graph to tensorRT
# ---------------------------------------------------------------

# For this inference test, all test data is randomly picked.
# If you want to use real data, just rewrite the defination of SAMPLES
import torch
from ppq import *
from ppq.api import *
from tqdm import tqdm

QUANT_PLATFROM = TargetPlatform.TRT_INT8
MODEL          = 'model.onnx'
INPUT_SHAPE    = [1, 3, 224, 224]
SAMPLES        = [torch.rand(size=INPUT_SHAPE) for _ in range(256)] # rewirte this to use real data.
DEVICE         = 'cuda'
FINETUNE       = True
QS             = QuantizationSettingFactory.default_setting()
EXECUTING_DEVICE = 'cuda'
REQUIRE_ANALYSE  = True

# -------------------------------------------------------------------
# 下面向你展示了常用参数调节选项：
# -------------------------------------------------------------------
QS.lsq_optimization = FINETUNE                                  # 启动网络再训练过程，降低量化误差
QS.lsq_optimization_setting.steps = 500                         # 再训练步数，影响训练时间，500 步大概几分钟
QS.lsq_optimization_setting.collecting_device = 'cuda'          # 缓存数据放在那，cuda 就是放在 gpu，如果显存超了你就换成 'cpu'
QS.dispatching_table.append(operation='OP NAME', platform=TargetPlatform.FP32) # 你可以把量化的不太好的算子送回 FP32

print('正准备量化你的网络，检查下列设置:')
print(f'TARGET PLATFORM      : {QUANT_PLATFROM.name}')
print(f'NETWORK INPUTSHAPE   : {INPUT_SHAPE}')

# ENABLE CUDA KERNEL 会加速量化效率 3x ~ 10x，但是你如果没有装相应编译环境的话是编译不了的
# 你可以尝试安装编译环境，或者在不启动 CUDA KERNEL 的情况下完成量化：移除 with ENABLE_CUDA_KERNEL(): 即可
with ENABLE_CUDA_KERNEL():
    qir = quantize_onnx_model(
        onnx_import_file=MODEL, calib_dataloader=SAMPLES, calib_steps=128, setting=QS,
        input_shape=INPUT_SHAPE, collate_fn=lambda x: x.to(EXECUTING_DEVICE), 
        platform=QUANT_PLATFROM, do_quantize=True)

    # -------------------------------------------------------------------
    # PPQ 计算量化误差时，使用信噪比的倒数作为指标，即噪声能量 / 信号能量
    # 量化误差 0.1 表示在整体信号中，量化噪声的能量约为 10%
    # 你应当注意，在 graphwise_error_analyse 分析中，我们衡量的是累计误差
    # 网络的最后一层往往都具有较大的累计误差，这些误差是其前面的所有层所共同造成的
    # 你需要使用 layerwise_error_analyse 逐层分析误差的来源
    # -------------------------------------------------------------------
    print('正计算网络量化误差(SNR)，最后一层的误差应小于 0.1 以保证量化精度:')
    reports = graphwise_error_analyse(
        graph=qir, running_device=EXECUTING_DEVICE, steps=32,
        dataloader=SAMPLES, collate_fn=lambda x: x.to(EXECUTING_DEVICE))
    for op, snr in reports.items():
        if snr > 0.1: ppq_warning(f'层 {op} 的累计量化误差显著，请考虑进行优化')

    print('网络量化结束，正在生成目标文件:')
    export_ppq_graph(
        graph=qir, platform=TargetPlatform.TRT_INT8,
        graph_save_to = 'model_int8.onnx', 
        config_save_to='model_int8.json')

    # -------------------------------------------------------------------
    # 完成量化后，你可以使用 create_engine.py 生成 trt engine.
    # 它位于 ppq / samples / TensorRT 文件夹下
    # 你可以使用 trt_infer.py 文件执行 engine 的推理
    # 你可以使用 Example_Profiling.py 文件执行 engine 的性能分析
    # -------------------------------------------------------------------