"""Copy a CLOSED legacy workbench to a new encrypted directory. Never in-place."""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import uuid
import zlib
from pathlib import Path

from .c_api import DBError
from .fileio import atomic_write
from .protection import FileProtector,page_context,wal_context
from .module_a.pages import HEADER as PAGE_HEADER,decode
from .contracts import PageKey


def migrate(source,destination):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if source==destination or source in destination.parents or destination.exists():
        raise DBError("INVALID_DESTINATION","目标必须是原目录之外的全新目录")
    if (source/"security"/"users.vault").exists():
        raise DBError("ALREADY_PROTECTED","源目录已经加密，应按完整目录备份")
    old_users=source/"security"/"users.json"
    if not old_users.exists() or not (source/"database"/"a_storage"/"storage.json").exists():
        raise DBError("INVALID_SOURCE","找不到完整的旧版工作台账号和数据库")
    lock=(source/"database"/"contract_runtime.lock").open("r+b")
    try:
        if os.name=="nt":
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc:
                raise DBError("DATABASE_BUSY","请关闭旧工作台后再转换") from exc
        else:
            raise DBError("PLATFORM_PROTECTION_REQUIRED","请在原 Windows 账号下转换")
        stage=destination.parent/("."+destination.name+".migrating-"+uuid.uuid4().hex)
        stage.mkdir(parents=True,mode=0o700)
        protector=FileProtector(stage)
        users=json.loads(old_users.read_text("utf-8"))
        for user in users["users"].values():
            user["algorithm"]="pbkdf2-sha256"
        atomic_write(stage/"security"/"users.vault",protector.seal(json.dumps({"format":"edudb-identities-v2",**users},ensure_ascii=False).encode(),b"identities"))
        count=0
        for path in (source/"database"/"a_storage"/"pages").rglob("*.page"):
            raw=path.read_bytes()
            size=PAGE_HEADER.unpack_from(raw)[2]
            body=json.loads(raw[PAGE_HEADER.size:PAGE_HEADER.size+size])
            key=PageKey(**body["key"])
            decode(raw,key)
            atomic_write(stage/path.relative_to(source),protector.seal(raw,page_context(key)))
            count+=1
        atomic_write(stage/"database"/"a_storage"/"storage.json",(source/"database"/"a_storage"/"storage.json").read_bytes())
        old_wal=source/"database"/"wal"/"contract.wal"
        if not old_wal.exists():
            raise DBError("WAL_NOT_FOUND","必须同时具有原数据库及事务日志")
        frames=bytearray();lsn=0
        with old_wal.open("rb") as stream:
            while header:=stream.read(8):
                if len(header)<8: break
                size,crc=struct.unpack(">II",header)
                if not 0<size<=32*1024*1024: raise DBError("WAL_CORRUPTION","事务日志帧长度损坏")
                raw=stream.read(size)
                if len(raw)<size: break
                if zlib.crc32(raw)!=crc: raise DBError("WAL_CORRUPTION","事务日志校验失败")
                lsn+=1
                if json.loads(raw)["lsn"]!=lsn: raise DBError("WAL_CORRUPTION","事务日志序号不连续")
                sealed=protector.seal(raw,wal_context(lsn))
                frames.extend(struct.pack(">II",len(sealed),zlib.crc32(sealed))+sealed)
        atomic_write(stage/"database"/"wal"/"contract.wal",bytes(frames))
        atomic_write(stage/"database"/"protected.json",b'{"format":"edudb-protected-v1"}')
        old_audit=source/"security"/"audit.jsonl"
        audit=[]
        if old_audit.exists():
            for number,line in enumerate(old_audit.read_text("utf-8").splitlines(),1):
                audit.append(base64.b64encode(protector.seal(line.encode(),f"audit:{number}".encode())))
        atomic_write(stage/"security"/"audit.vault",b"\n".join(audit)+(b"\n" if audit else b""))
        # Migration validation is not a server startup: preserve copied account
        # credentials until the user actually starts the new workbench.
        from .workbench.security import IdentityStore
        from .workbench.audit import AuditLog
        from .integration.contract_runtime import ContractRuntime
        IdentityStore(stage/"security"/"users.vault",protector)
        AuditLog(stage/"security"/"audit.vault",protector)
        with ContractRuntime(stage/"database",protector=protector):
            pass
        stage.rename(destination)
        return dict(destination=str(destination),pages=count,log_records=lsn,source_unchanged=True)
    finally:
        lock.close()


def main():
    parser=argparse.ArgumentParser(description="将旧工作台复制为新加密目录，原目录不变")
    parser.add_argument("source",type=Path)
    parser.add_argument("destination",type=Path)
    args=parser.parse_args()
    print(json.dumps(migrate(args.source,args.destination),ensure_ascii=False,indent=2))


if __name__=="__main__": main()
