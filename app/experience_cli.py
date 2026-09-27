"""Offline operator interface. Does not send to external AI."""
import argparse,json,time
from pathlib import Path
from app.experience_memory import ExperienceMemory


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--project',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('list');sub.add_parser('stats')
    a=sub.add_parser('add');a.add_argument('file',type=Path)
    r=sub.add_parser('review');r.add_argument('id');r.add_argument('--status',choices=['verified','revoked'],required=True)
    r.add_argument('--reviewer',required=True);r.add_argument('--proof',required=True);r.add_argument('--days',type=int,default=30)
    i=sub.add_parser('index');i.add_argument('--config',type=Path,required=True)
    args=p.parse_args();config=json.loads(args.config.read_text(encoding='utf-8-sig')) if args.command=='index' else {}
    service=ExperienceMemory(args.root,config)
    if args.command=='list':result=service.store.list(args.project)
    elif args.command=='stats':result=service.store.stats(args.project)
    elif args.command=='add':
        data=json.loads(args.file.read_text(encoding='utf-8-sig'))
        result={'id':service.store.add(args.project,data['kind'],data['content'],data.get('applicability',{}),data['evidence'])}
    elif args.command=='review':
        service.store.review(args.project,args.id,args.status,args.reviewer,args.proof,time.time()+args.days*86400)
        result={'id':args.id,'status':args.status}
    else:result={'indexed':service.reindex(args.project)}
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
