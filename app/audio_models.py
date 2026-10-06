import asyncio,numpy as np,sounddevice as sd
class VAD:
 def __init__(self,t):self.t=t
 def speech(self,x):return bool(x.size and np.sqrt(np.mean(np.square(x.astype(np.float32))))>=self.t)
class Microphone:
 def __init__(self,rate,ms):self.rate=rate;self.frames=rate*ms//1000
 async def chunks(self):
  loop=asyncio.get_running_loop();q=asyncio.Queue(maxsize=64)
  def put(x):
   if not q.full():q.put_nowait(x)
  def cb(data,*args):loop.call_soon_threadsafe(put,data[:,0].copy())
  with sd.InputStream(samplerate=self.rate,channels=1,dtype="float32",blocksize=self.frames,callback=cb):
   while True:yield await q.get()
class Speaker:
 async def play(self,audio,rate):await asyncio.to_thread(sd.play,audio,rate,True)
 def stop(self):sd.stop()
class QwenASR:
 def __init__(self,name):
  import torch
  from qwen_asr import Qwen3ASRModel
  self.model=Qwen3ASRModel.from_pretrained(name,dtype=torch.bfloat16,device_map="cuda",max_new_tokens=256)
 async def transcribe(self,audio,rate):
  x=await asyncio.to_thread(self.model.transcribe,audio=(audio,rate),language="Japanese");x=x[0] if isinstance(x,list) else x
  return str(getattr(x,"text",x)).strip()
class QwenTTS:
 def __init__(self,name,speaker):
  import torch
  from qwen_tts import Qwen3TTSModel
  self.speaker=speaker;self.model=Qwen3TTSModel.from_pretrained(name,device_map="cuda:0",dtype=torch.bfloat16,attn_implementation="sdpa")
 async def synthesize(self,text):
  w,r=await asyncio.to_thread(self.model.generate_custom_voice,text=text,language="Japanese",speaker=self.speaker,instruct="自然で落ち着いた声で話してください。")
  return np.asarray(w[0],dtype=np.float32),int(r)
