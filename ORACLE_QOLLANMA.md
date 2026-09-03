# Oracle Cloud uchun o'rnatish

Tavsiya etilgan VM:

- Shape: VM.Standard.A1.Flex (Always Free)
- CPU: 2 OCPU
- RAM: 12 GB
- Image: Ubuntu 24.04 yoki 22.04
- Boot volume: 50 GB

Serverga SSH orqali kirgach:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
git clone https://github.com/a72693161-wq/uzum-video-bot.git
cd uzum-video-bot
cp .env.example .env
nano .env
```

`.env` ichida BOT_TOKEN qiymatini haqiqiy token bilan almashtiring. Hammaga ishlashi uchun ALLOWED_USER_ID qiymatini bo'sh qoldiring.

Saqlash: Ctrl+O, Enter. Chiqish: Ctrl+X.

Botni ishga tushirish:

```bash
sudo docker compose up -d --build
sudo docker compose logs -f
```

Yangilash:

```bash
cd uzum-video-bot
git pull
sudo docker compose up -d --build
```

To'xtatish:

```bash
sudo docker compose down
```
