"""Windows-user-bound authenticated file encryption for the local workbench.

Protects copied files/offline inspection. Does NOT isolate an attacker running
code as the same Windows user, who can also call DPAPI. No hard-coded secret.
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from .c_api import DBError
from .fileio import atomic_write

MAGIC=b"EDENC1"


def _dpapi(data: bytes, decrypt=False) -> bytes:
    if os.name!="nt":
        raise DBError("PLATFORM_PROTECTION_REQUIRED","此版本的磁盘保护需要 Windows 用户密钥服务")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_=[("size",wintypes.DWORD),("data",ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    output=Blob()
    crypt=ctypes.WinDLL("crypt32",use_last_error=True)
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    function=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,
                       ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    function.restype=wintypes.BOOL
    if not function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output)):
        raise DBError("KEY_UNAVAILABLE","无法使用当前 Windows 账号解锁数据密钥，请使用原账号及完整备份")
    try:
        return ctypes.string_at(output.data,output.size)
    finally:
        kernel.LocalFree(output.data)


class FileProtector:
    def __init__(self,root):
        self.root=Path(root)
        keyfile=self.root/"security"/"key.dpapi"
        if keyfile.exists():
            key=_dpapi(keyfile.read_bytes(),True)
        else:
            if (self.root/"database"/"a_storage"/"storage.json").exists():
                raise DBError("MIGRATION_REQUIRED","旧数据库未加密，或密钥已丢失。请保留原目录，转换到新的保护目录")
            key=secrets.token_bytes(32)
            keyfile.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            wrapped=_dpapi(key)
            try:
                descriptor=os.open(keyfile,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_BINARY",0),0o600)
            except FileExistsError:
                key=_dpapi(keyfile.read_bytes(),True)
            else:
                try:
                    view=memoryview(wrapped)
                    while view:
                        written=os.write(descriptor,view)
                        if not written: raise OSError("密钥文件写入不完整")
                        view=view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        if len(key)!=32:
            raise DBError("KEY_UNAVAILABLE","密钥格式不正确，不能自动重建")
        self._cipher=AESGCM(key)

    def seal(self,data:bytes,context:bytes) -> bytes:
        nonce=secrets.token_bytes(12)
        return MAGIC+nonce+self._cipher.encrypt(nonce,data,context)

    def open(self,data:bytes,context:bytes) -> bytes:
        if not data.startswith(MAGIC) or len(data)<len(MAGIC)+28:
            raise DBError("ENCRYPTION_REQUIRED","文件未受保护或格式不正确，禁止当作新文件覆盖")
        nonce=data[len(MAGIC):len(MAGIC)+12]
        try:
            return self._cipher.decrypt(nonce,data[len(MAGIC)+12:],context)
        except InvalidTag as exc:
            raise DBError("FILE_AUTHENTICATION_FAILED","文件被修改、位置不匹配或密钥不正确") from exc


def page_context(key):
    return f"page:{key.file_id}:{key.page_id}".encode()


def wal_context(lsn):
    return f"wal:{lsn}".encode()
