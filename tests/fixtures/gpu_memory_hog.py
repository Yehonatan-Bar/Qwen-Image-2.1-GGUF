"""
Test helper: holds a block of GPU memory to simulate another program filling the card.

Usage:  python tests/fixtures/gpu_memory_hog.py <megabytes> <seconds>
Uses the CUDA driver API (nvcuda.dll) directly, so no packages are needed.
"""
import ctypes
import sys
import time


def main():
    megabytes_to_hold = int(sys.argv[1])
    seconds_to_hold = int(sys.argv[2])
    cuda_driver = ctypes.WinDLL("nvcuda.dll")
    if cuda_driver.cuInit(0) != 0:
        print("cuInit failed")
        return 1
    device = ctypes.c_int()
    cuda_driver.cuDeviceGet(ctypes.byref(device), 0)
    context = ctypes.c_void_p()
    cuda_driver.cuCtxCreate_v2(ctypes.byref(context), 0, device)
    device_pointer = ctypes.c_uint64()
    allocation_result = cuda_driver.cuMemAlloc_v2(ctypes.byref(device_pointer), ctypes.c_size_t(megabytes_to_hold * 1024 * 1024))
    print(f"holding {megabytes_to_hold} MB (cuMemAlloc result {allocation_result}) for {seconds_to_hold}s", flush=True)
    time.sleep(seconds_to_hold)
    cuda_driver.cuMemFree_v2(device_pointer)
    cuda_driver.cuCtxDestroy_v2(context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
