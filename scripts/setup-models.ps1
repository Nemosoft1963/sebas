param([switch]$ASR,[switch]$TTS)
if($ASR){& .\.venv\Scripts\python.exe -m pip install -U qwen-asr}
if($TTS){& .\.venv\Scripts\python.exe -m pip install -U qwen-tts}
