# Homelab

Self-hosted media, automation, tools and Telegram bots on a headless Debian
laptop, run with Docker Compose.

## Services

| Service | What it does | Port |
|---|---|---|
| Jellyfin | Media server | 8096 |
| qBittorrent | Torrent client | 8080 |
| Prowlarr | Indexer manager for Radarr/Sonarr | 9696 |
| Radarr | Movie automation | 7878 |
| Sonarr | TV automation | 8989 |
| Bazarr | Automatic subtitles | 6767 |
| Seerr | Request page (search and request titles) | 5055 |
| FlareSolverr | Helps Prowlarr with Cloudflare-protected indexers | 8191 |
| Pi-hole | DNS ad blocking (admin page) | 8081 |
| Vaultwarden | Password manager (needs HTTPS, see below) | 8082 |
| Uptime Kuma | Uptime monitoring | 3001 |
| Homepage | Dashboard | 3000 |
| Stirling PDF | PDF tools | 8083 |
| telegram-bot-api | Local Telegram Bot API server (2 GB files) | internal |
| jellyfin-bot | Telegram bot: forward a file, it is saved to the server | none |
| ytdl-bot | Telegram bot: YouTube links to MP3/MP4 | none |
| access-bot | Telegram bot: approves who may use the other bots | none |

Open any service at `http://<server-ip>:<port>`. Away from home, use the
server's Tailscale IP instead. Some services in `docker-compose.yml` may be
commented out (for example Pi-hole); uncomment them when you want them.

## Folder layout

```
homelab/
├── docker-compose.yml
├── .env                  secrets (never committed)
├── .env.sample           template for .env (committed)
├── .gitignore
├── README.md
├── access-bot/           bot.py  Dockerfile  requirements.txt
├── telegram-bot/         bot.py  Dockerfile  requirements.txt
├── ytdl-bot/             bot.py  Dockerfile  requirements.txt
└── config/               app data, created on this machine (not committed)
```

Media and downloads live outside the repo, on the storage drive:

```
/storage/
├── downloads/            qBittorrent downloads
└── media/
    ├── movies/           Jellyfin movies library
    └── tv/               Jellyfin TV library
```

`downloads` and `media` are on the same drive so Radarr and Sonarr can
hardlink finished downloads (instant, no extra disk space).

## Setting up from scratch

### 1. Base system

Install Debian, then:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y curl git openssh-server
```

Stop the laptop suspending when the lid is closed:

```bash
sudo nano /etc/systemd/logind.conf
# remove the leading # and set:
#   HandleLidSwitch=ignore
#   HandleLidSwitchExternalPower=ignore
sudo systemctl restart systemd-logind
```

### 2. Storage drive

```bash
lsblk                                   # find the drive, e.g. /dev/sdb1
sudo mkfs.ext4 /dev/sdb1                # ONLY for a new drive: this erases it
sudo blkid /dev/sdb1                    # copy the UUID
sudo mkdir -p /storage
echo 'UUID=your-uuid-here /storage ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a
sudo chown -R $USER:$USER /storage
mkdir -p /storage/downloads /storage/media/movies /storage/media/tv
df -h /storage                          # confirm it is mounted
```

Always check the drive is mounted before starting containers. If `/storage`
is missing at boot, containers write to the laptop's own disk instead.

### 3. Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                       # open the printed link on another device
tailscale status
```

### 4. Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out and back in, then check:
docker run --rm hello-world
```

### 5. Clone this repo

```bash
cd ~
git clone https://github.com/rohithr99/homelab.git
cd homelab
```

### 6. Create your .env

```bash
cp .env.sample .env
nano .env
```

Fill in every value (see the table below). `.env` is ignored by git, so your
secrets stay on this machine.

### 7. Create the config folders

Run this from inside `~/homelab`. Do not put a `/` in front of `config`.

```bash
mkdir -p config/{homepage,uptime-kuma,vaultwarden,qbittorrent,jellyfin,prowlarr,radarr,sonarr,bazarr,seerr,pihole,shared}
mkdir -p config/stirling-pdf/{tessdata,configs}
```

Do not create `config/telegram-bot-api` yourself. Let Docker create it on
first start, otherwise the Bot API server can hit permission errors.

Check the user ID matches the `PUID`/`PGID` values (1000) in the compose file:

```bash
id
```

Check the hardware transcoding device exists. If this folder is missing,
delete the `devices:` line from the Jellyfin service:

```bash
ls /dev/dri
```

### 8. Start everything

```bash
docker compose up -d --build
docker ps
```

Every container should show `Up`. If one keeps restarting, read its log:

```bash
docker logs <container-name>
```

## Environment variables (.env)

```
# Telegram local Bot API server (from https://my.telegram.org -> API development tools)
API_ID=
API_HASH=

# Bot tokens (from @BotFather, one per bot)
BOT_TOKEN=            # jellyfin-bot (file saver)
YTDL_BOT_TOKEN=       # ytdl-bot (YouTube)
ACCESS_BOT_TOKEN=     # access-bot (approvals)
ACCESS_BOT_NAME=@your_access_bot_username

# Telegram numeric IDs of admins, comma separated (get yours from @userinfobot)
ADMIN_IDS=

# Jellyfin: Dashboard -> API Keys. Leave empty to skip auto library refresh.
JELLYFIN_API_KEY=

# Pi-hole admin password
PIHOLE_PASSWORD=
```

## First-time wiring

1. **qBittorrent:** `docker logs qbittorrent | grep -i password` for the
   temporary login (user `admin`), then set a permanent password under
   Tools > Options > WebUI.
2. **Radarr / Sonarr:** add qBittorrent as a download client (host
   `qbittorrent`, port `8080`). Root folders: `/data/media/movies` and
   `/data/media/tv`.
3. **Prowlarr:** add your indexers. Under Settings > Apps connect Radarr
   (`http://radarr:7878`) and Sonarr (`http://sonarr:8989`) using their API keys.
4. **Jellyfin:** add libraries `/data/media/movies` and `/data/media/tv`.
5. **Bazarr:** set a language profile, enable providers, connect Radarr and Sonarr.
6. **Seerr:** sign in with Jellyfin (`http://jellyfin:8096`, leave URL Base
   empty), then add Radarr and Sonarr.

Inside Docker, services reach each other by container name (for example
`radarr`), never `localhost`.

## Telegram bots

- Press `/start` in the **access bot** as an admin first, so it can message you.
- New people send `/start` to the access bot. Admins get Approve / Deny
  buttons. Approved users can use all the bots.
- Admin commands: `/pending`, `/users`, `/adduser <id> [name]`, `/removeuser <id>`.
- The user list is a SQLite file at `config/shared/users.db`.

## Vaultwarden needs HTTPS

The web vault only works over HTTPS. Use Tailscale to provide it
(enable MagicDNS and HTTPS Certificates in the Tailscale admin console):

```bash
sudo tailscale serve --bg 8082
tailscale serve status
```

Then set `DOMAIN` in the Vaultwarden service to the HTTPS address it prints.

## Updating

```bash
cd ~/homelab
git pull
docker compose pull
docker compose up -d --build
```

yt-dlp breaks when YouTube changes, so rebuild that bot now and then:

```bash
docker compose build --no-cache ytdl-bot && docker compose up -d ytdl-bot
```

## Backups

Git only stores the setup (compose file, bot code). It does not store your
data. Back up `config/` (especially `config/vaultwarden`) and `.env`
separately, to another drive or off the machine, with a tool such as restic.
Do not commit them to git.

## Troubleshooting

- **Permission denied inside `config/`:** Docker created the folder as root.
  Fix with `sudo chown -R $USER:$USER config/<folder>`.
- **`mkdir: cannot create directory '/config'`:** remove the leading `/`.
- **Radarr/Sonarr never import finished downloads:** the qBittorrent,
  Radarr and Sonarr download paths do not match. All three must see the
  same `/downloads` folder.
- **Port already in use:** another service is using that port. Change the left
  side of the `ports:` line (for example `"8083:8080"`).
- **Telegram bot ignores someone:** they are not approved yet. Check `/users`
  in the access bot.

