@echo off
rem Creates .venv with the GPU build of onnxruntime.
rem insightface depends on the CPU "onnxruntime" package, which shadows
rem onnxruntime-gpu, so it is removed and the GPU build reinstalled afterwards.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3.12 -m venv .venv || exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-dev.txt || exit /b 1
".venv\Scripts\python.exe" -m pip uninstall -y onnxruntime onnxruntime-gpu
".venv\Scripts\python.exe" -m pip install "onnxruntime-gpu[cuda,cudnn]==1.31.1" || exit /b 1
".venv\Scripts\python.exe" -c "import onnxruntime as o; o.preload_dlls(); p=o.get_available_providers(); print(p); raise SystemExit(0 if 'CUDAExecutionProvider' in p else 1)"
