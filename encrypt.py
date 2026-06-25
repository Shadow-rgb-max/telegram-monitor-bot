from cryptography.fernet import Fernet

key = open('config.key', 'rb').read()
f = Fernet(key)

with open('config.ini', 'rb') as fin:
    data = fin.read()

if data.startswith(b'ENCRYPTED\n'):
    encrypted_data = data[10:]
else:
    raise ValueError("Неверный формат файла")

decrypted_data = f.decrypt(encrypted_data)

# Перезаписываем оригинальный файл
with open('config.ini', 'wb') as fout:
    fout.write(decrypted_data)

print("Файл config.ini расшифрован!")
