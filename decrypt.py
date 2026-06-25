from cryptography.fernet import Fernet
key = open('config.key', 'rb').read()
f = Fernet(key)
with open('config.ini', 'rb') as fin:
    data = fin.read()
enc = f.encrypt(data)
with open('config.ini', 'wb') as fout:
    fout.write(b'ENCRYPTED\n' + enc)
