#!/bin/bash
# Pterodactyl Panel + Wings auto installer for idx.google.com
# Run as root (or sudo bash install-ptero.sh)

### SETTINGS ###
PANEL_DOMAIN="idx.google.com"
DB_NAME="panel"
DB_USER="pterouser"
DB_PASS="SuperSecurePassword123!"
ADMIN_EMAIL="admin@$PANEL_DOMAIN"
ADMIN_USER="admin"
ADMIN_PASS="ChangeMe123!"
################

set -e

echo "[+] Updating system..."
apt update && apt upgrade -y

echo "[+] Installing dependencies..."
apt install -y curl wget git unzip tar jq ca-certificates lsb-release apt-transport-https software-properties-common gnupg2

echo "[+] Installing PHP..."
add-apt-repository ppa:ondrej/php -y
apt update
apt install -y php8.3 php8.3-cli php8.3-fpm php8.3-mysql php8.3-xml php8.3-mbstring php8.3-curl php8.3-zip php8.3-bcmath php8.3-json php8.3-gd php8.3-intl composer

echo "[+] Installing MariaDB and Redis..."
apt install -y mariadb-server redis-server

echo "[+] Configuring MariaDB..."
mysql -u root <<MYSQL_SCRIPT
CREATE DATABASE IF NOT EXISTS $DB_NAME;
CREATE USER IF NOT EXISTS '$DB_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_USER'@'127.0.0.1';
FLUSH PRIVILEGES;
MYSQL_SCRIPT

echo "[+] Installing Docker..."
curl -sSL https://get.docker.com/ | CHANNEL=stable bash
systemctl enable --now docker

echo "[+] Installing Pterodactyl Panel..."
cd /var/www
git clone https://github.com/pterodactyl/panel.git panel
cd panel
# Optionally check out latest tag
git checkout $(git describe --tags $(git rev-list --tags --max-count=1))
cp .env.example .env
composer install --no-dev --optimize-autoloader
php artisan key:generate

echo "[+] Configuring .env for panel..."
sed -i "s/DB_DATABASE=.*/DB_DATABASE=$DB_NAME/" .env
sed -i "s/DB_USERNAME=.*/DB_USERNAME=$DB_USER/" .env
sed -i "s/DB_PASSWORD=.*/DB_PASSWORD=$DB_PASS/" .env
sed -i "s|APP_URL=.*|APP_URL=https://$PANEL_DOMAIN|" .env

echo "[+] Running migrations & seeding..."
php artisan migrate --seed --force
php artisan storage:link

echo "[+] Creating admin user..."
php artisan p:user:make --email=$ADMIN_EMAIL --username=$ADMIN_USER --name-first=Admin --name-last=User --password=$ADMIN_PASS --admin=1 || true

echo "[+] Setting file permissions..."
chown -R www-data:www-data /var/www/panel
chmod -R 755 /var/www/panel

echo "[+] Setting up Nginx..."
apt install -y nginx certbot python3-certbot-nginx

cat > /etc/nginx/sites-available/pterodactyl.conf <<EOF
server {
    listen 80;
    server_name $PANEL_DOMAIN;

    root /var/www/panel/public;
    index index.php;

    client_max_body_size 100m;

    location / {
        try_files \$uri \$uri/ /index.php?\$query_string;
    }

    location ~ \.php\$ {
        fastcgi_split_path_info ^(.+\.php)(/.+)\$;
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
        fastcgi_index index.php;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME \$realpath_root\$fastcgi_script_name;
        fastcgi_param DOCUMENT_ROOT \$realpath_root;
    }

    location ~ /\.ht {
        deny all;
    }
}
EOF

ln -s /etc/nginx/sites-available/pterodactyl.conf /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo "[+] Obtaining SSL certificate..."
certbot --nginx -d $PANEL_DOMAIN --non-interactive --agree-tos -m $ADMIN_EMAIL --redirect

echo "[+] Setting up cron job for schedule..."
(crontab -l 2>/dev/null; echo "* * * * * php /var/www/panel/artisan schedule:run >> /dev/null 2>&1") | crontab -

echo "[+] Installing Wings..."
mkdir -p /etc/pterodactyl
cd /etc/pterodactyl
curl -Lo wings https://github.com/pterodactyl/wings/releases/latest/download/wings_linux_amd64
chmod +x wings

echo "[+] Creating systemd service for Wings..."
cat > /etc/systemd/system/wings.service <<EOF
[Unit]
Description=Pterodactyl Wings Daemon
After=docker.service
Requires=docker.service

[Service]
User=root
WorkingDirectory=/etc/pterodactyl
ExecStart=/etc/pterodactyl/wings
Restart=on-failure
StartLimitIntervalSec=600

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wings
systemctl start wings

echo "======================================"
echo "[✓] Installation complete!"
echo "Panel: https://$PANEL_DOMAIN"
echo "Admin login: $ADMIN_EMAIL / $ADMIN_PASS"
echo "======================================"
