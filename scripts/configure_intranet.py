"""Configure the actual intranet host, preserving employees, secrets and dry-run mode."""
import argparse
from datetime import datetime
import ipaddress
import os
from pathlib import Path
import secrets
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def configure(address, port, root=ROOT):
    ip = ipaddress.ip_address(address)
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        raise ValueError('请填写运行电脑的固定内网 IPv4 地址')
    if not 1 <= port <= 65535:
        raise ValueError('端口无效')
    env = root / '.env'
    original = env.read_text(encoding='utf-8-sig') if env.exists() else ''
    values = {}
    for line in original.splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    address_url = f'http://{address}:{port}'
    updates = {'HOST': '0.0.0.0', 'PORT': str(port), 'POSTER_BASE_URL': address_url,
               'ADMIN_TOKEN': values.get('ADMIN_TOKEN') or secrets.token_urlsafe(32)}
    # Never switch a working environment from simulation to real delivery here.
    if 'DRY_RUN' not in values:
        updates['DRY_RUN'] = 'true'
    lines, written = [], set()
    for line in original.splitlines():
        key = line.split('=', 1)[0].strip()
        if '=' in line and key in updates:
            if key not in written:
                lines.append(key + '=' + updates[key])
                written.add(key)
        else:
            lines.append(line)
    lines += [key + '=' + value for key, value in updates.items() if key not in written]
    if original:
        (root / ('.env.before-lan.' + datetime.now().strftime('%Y%m%d%H%M%S%f'))).write_text(original, encoding='utf-8')
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root, prefix='.env.', suffix='.tmp', delete=False) as handle:
            pending = handle.name
            handle.write('\n'.join(lines) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, env)
    finally:
        if pending and os.path.exists(pending):
            os.unlink(pending)
    return address_url


def main():
    parser = argparse.ArgumentParser(description='在实际运行后台的电脑配置内网访问。不会发送消息或更改演练模式。')
    parser.add_argument('--address', required=True)
    parser.add_argument('--port', type=int, default=8848)
    args = parser.parse_args()
    url = configure(args.address, args.port)
    from app import db, delivery_config
    db.init_db()
    cfg = delivery_config.current()
    delivery_config.save({'base_url': url, 'auto_schedule': cfg['auto_schedule']})
    print('已配置内网地址：' + url)
    print('请使用既有方式重启后台。管理口令保存在本机 .env 的 ADMIN_TOKEN，未输出到日志。')
    print('员工资料、飞书凭据及原有演练模式均已保留。')


if __name__ == '__main__':
    main()
