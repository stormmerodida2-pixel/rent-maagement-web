"""Encrypt backups in chunks so large dumps never have to fit in memory.

File format: MAGIC, then repeated [4-byte big-endian length][Fernet token for up to CHUNK bytes].
Each token is authenticated, so any tampering or a wrong key is detected on decryption.
"""

import struct

from cryptography.fernet import Fernet, InvalidToken

MAGIC = b'RENTALS-BACKUP-1\n'
CHUNK = 8 * 1024 * 1024


class BackupKeyError(Exception):
    pass


def encrypt_file(src, dst, key):
    fernet = Fernet(key)
    with open(src, 'rb') as fin, open(dst, 'wb') as fout:
        fout.write(MAGIC)
        while chunk := fin.read(CHUNK):
            token = fernet.encrypt(chunk)
            fout.write(struct.pack('>I', len(token)))
            fout.write(token)


def decrypt_file(src, dst, key):
    fernet = Fernet(key)
    with open(src, 'rb') as fin, open(dst, 'wb') as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise BackupKeyError(f'{src} is not an encrypted backup made by this app.')
        while header := fin.read(4):
            (length,) = struct.unpack('>I', header)
            try:
                fout.write(fernet.decrypt(fin.read(length)))
            except InvalidToken:
                raise BackupKeyError('Wrong BACKUP_ENCRYPTION_KEY, or the file is damaged.') from None
