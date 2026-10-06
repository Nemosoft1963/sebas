import argparse,asyncio
from pathlib import Path
import numpy as np,yaml
from app.core import Controller,Memory,Ollama,State
from app.audio_models import Microphone,Speaker,VAD,QwenASR,QwenTTS
ROOT=Path(__file__).resolve().parents[1]
def cfg():
 with (ROOT/"config/settings.yaml").open(encoding="utf-8-sig") as f:return yaml.safe_load(f)
def build(c,voice):
 llm=Ollama(c["llm"]["url"],c["llm"]["model"]);tts=QwenTTS(c["tts"]["model"],c["tts"]["speaker"]) if voice else None
 return llm,Controller(llm,Memory(ROOT/"data/memory/conversations.db"),c["system_prompt"],tts,Speaker() if voice else None)
async def text_chat(ctrl,speak):
 while True:
  s=await asyncio.to_thread(input,"あなた> ")
  if s in ("/quit","/exit"):return
  print("AI>",await ctrl.respond(s,speak))
async def voice_chat(ctrl,c):
 a=c["audio"];mic=Microphone(a["sample_rate"],a["block_ms"]);vad=VAD(a["threshold"]);stt=QwenASR(c["asr"]["model"]);buf=[];talk=False;sm=qm=0;response=None
 print("音声会話開始（Ctrl+Cで終了）")
 async for x in mic.chunks():
  if vad.speech(x):
   if not talk:talk=True;sm=qm=0;ctrl.barge_in()
   buf.append(x);sm+=a["block_ms"];qm=0
  elif talk:
   buf.append(x);qm+=a["block_ms"]
   if qm>=a["end_silence_ms"]:
    talk=False;audio=np.concatenate(buf);buf.clear()
    if sm<a["min_speech_ms"]:continue
    ctrl.state=State.TRANSCRIBING;s=await stt.transcribe(audio,a["sample_rate"])
    if s:
     print("あなた>",s)
     if response and not response.done():ctrl.barge_in()
     response=asyncio.create_task(ctrl.respond(s,True));response.add_done_callback(lambda t:print("AI>",t.result()) if not t.cancelled() else None)
async def run():
 p=argparse.ArgumentParser();p.add_argument("--speak",action="store_true");p.add_argument("--voice",action="store_true");p.add_argument("--check",action="store_true");a=p.parse_args();c=cfg();llm,ctrl=build(c,a.speak or a.voice)
 if a.check:print("Ollama:","OK" if await llm.health() else "NG");return
 await (voice_chat(ctrl,c) if a.voice else text_chat(ctrl,a.speak))
def main():asyncio.run(run())
if __name__=="__main__":main()
