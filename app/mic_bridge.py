from __future__ import annotations
import io, os, threading, wave
import httpx, numpy as np, sounddevice as sd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app=FastAPI(title="Local Mic Bridge")
app.add_middleware(CORSMiddleware,allow_origins=["http://localhost:8099","http://127.0.0.1:8099"],allow_methods=["*"],allow_headers=["*"])
_lock=threading.Lock();_stream=None;_chunks=[];_last_peak=0.0;_last_rms=0.0;_level_sum=0.0;_level_count=0
DEVICE_QUERY=os.getenv("MIC_DEVICE","HD Pro Webcam C920").lower()
DEVICE_INDEX=next((i for i,d in enumerate(sd.query_devices()) if d["max_input_channels"]>0 and DEVICE_QUERY in d["name"].lower() and sd.query_hostapis(d["hostapi"])["name"]=="MME"),int(sd.default.device[0]))
DEVICE=sd.query_devices(DEVICE_INDEX);RATE=int(DEVICE["default_samplerate"])

@app.get("/health")
def health():
 try:return {"status":"ok","recording":_stream is not None,"device":DEVICE.get("name",""),"device_index":DEVICE_INDEX,"sample_rate":RATE,"peak":round(_last_peak,6),"rms":round(_last_rms,6)}
 except Exception as exc:return {"status":"error","recording":False,"error":str(exc)}

@app.post("/record/start")
def start():
 global _stream,_chunks,_last_peak,_last_rms,_level_sum,_level_count
 with _lock:
  if _stream is not None:return {"status":"recording"}
  _chunks=[];_last_peak=0.0;_last_rms=0.0;_level_sum=0.0;_level_count=0
  def callback(indata,frames,time,status):
   global _last_peak,_last_rms,_level_sum,_level_count
   if status:pass
   chunk=indata[:,0].copy();_chunks.append(chunk)
   if chunk.size:
    _last_peak=max(_last_peak,float(np.max(np.abs(chunk))))
    _level_sum+=float(np.sum(np.square(chunk,dtype=np.float64)));_level_count+=int(chunk.size)
    _last_rms=float(np.sqrt(_level_sum/_level_count))
  try:
   _stream=sd.InputStream(device=DEVICE_INDEX,samplerate=RATE,channels=1,dtype="float32",blocksize=1600,callback=callback);_stream.start()
  except Exception as exc:
   _stream=None;raise HTTPException(500,f"Microphone open failed: {exc}")
 return {"status":"recording"}

@app.post("/record/stop")
async def stop():
 global _stream,_chunks,_last_peak
 with _lock:
  if _stream is None:raise HTTPException(409,"Not recording")
  _stream.stop();_stream.close();_stream=None
  audio=np.concatenate(_chunks) if _chunks else np.array([],dtype=np.float32);_chunks=[]
 if audio.size==0:raise HTTPException(422,"Empty recording")
 peak=float(np.max(np.abs(audio))) if audio.size else 0.0
 rms=float(np.sqrt(np.mean(np.square(audio,dtype=np.float64)))) if audio.size else 0.0
 duration=float(audio.size/RATE)
 if peak>0.0001:audio=np.clip(audio*(0.85/peak),-1,1)
 pcm=(audio*32767).astype(np.int16);buf=io.BytesIO()
 with wave.open(buf,"wb") as wav:wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(RATE);wav.writeframes(pcm.tobytes())
 async with httpx.AsyncClient(timeout=180) as client:
  response=await client.post("http://127.0.0.1:8099/api/transcribe",files={"file":("recording.wav",buf.getvalue(),"audio/wav")})
 if response.is_error:
  try:detail=response.json().get("detail",response.text)
  except Exception:detail=response.text
  raise HTTPException(response.status_code,f"{detail} (peak={peak:.6f}, rms={rms:.6f}, duration={duration:.1f}s)")
 result=response.json();result["input_peak"]=round(peak,6);result["input_rms"]=round(rms,6);result["duration"]=round(duration,2);result["sample_rate"]=RATE;return result
