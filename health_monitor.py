#!/usr/bin/env python3
"""
NKFX Universal Health Monitor
============================
Monitors all NKFX services and sends Telegram alerts on failures.
Supports auto-discovery, auto-restart and daily summaries.

Usage:
    python3 health_monitor.py              # Run health check
    python3 health_monitor.py --daily      # Send daily summary
    python3 health_monitor.py --status     # Show current status
    python3 health_monitor.py --discover   # Show discovered containers
"""

import os
import sys
import json
import yaml
import subprocess
import requests
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============================================================
# CONFIGURATION
# ============================================================

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yml"
STATE_FILE = SCRIPT_DIR / "state.json"
LOG_FILE = Path("/root/nkfx/logs/health.log")

# Ensure log directory exists
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Default auto-discovery patterns
DEFAULT_AUTO_PATTERNS = ['nkfx', 'mt5', 'vnc', 'kraken', 'pancake', 'n8n', 'qdrant', 'cloudflare', 'bridge']
CRITICAL_PATTERNS = ['mt5', 'bridge', 'vnc', 'nkfx-smc', 'nkfx-me', 'nkfx-factory', 'n8n', 'cloudflare']


# ============================================================
# AUTO-DISCOVERY
# ============================================================

def discover_containers(patterns: List[str] = None) -> List[dict]:
    """
    Auto-discover all NKFX-related containers.
    No manual config needed for new bots.
    """
    if patterns is None:
        patterns = DEFAULT_AUTO_PATTERNS
    
    discovered = []
    seen_names = set()
    
    # 1. Get all running containers
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        running_containers = [c for c in result.stdout.strip().split('\n') if c]
    except Exception as e:
        log.error(f"Error listing containers: {e}")
        running_containers = []
    
    # 2. Auto-include containers matching patterns
    for container in running_containers:
        if any(pattern in container.lower() for pattern in patterns):
            if container not in seen_names:
                discovered.append({
                    'name': container,
                    'type': 'docker',
                    'critical': is_critical_container(container),
                    'auto_discovered': True,
                    'description': f'Auto-discovered: {container}'
                })
                seen_names.add(container)
    
    # 3. Also scan docker-compose files in /root/nkfx/bots/
    bots_dir = '/root/nkfx/bots'
    if os.path.exists(bots_dir):
        for bot_folder in os.listdir(bots_dir):
            bot_path = os.path.join(bots_dir, bot_folder)
            if not os.path.isdir(bot_path):
                continue
                
            compose_path = os.path.join(bot_path, 'docker-compose.yml')
            if os.path.exists(compose_path):
                try:
                    with open(compose_path) as f:
                        compose = yaml.safe_load(f)
                        for service_name, service_config in compose.get('services', {}).items():
                            # Use container_name if specified, otherwise service_name
                            container_name = service_config.get('container_name', service_name) if isinstance(service_config, dict) else service_name
                            if container_name not in seen_names:
                                discovered.append({
                                    'name': container_name,
                                    'type': 'docker',
                                    'critical': is_critical_container(container_name),
                                    'compose_path': bot_path,
                                    'auto_discovered': True,
                                    'description': f'From compose: {bot_folder}'
                                })
                                seen_names.add(container_name)
                except Exception as e:
                    log.debug(f"Error reading {compose_path}: {e}")
    
    # 4. Scan other compose locations
    other_compose_dirs = [
        '/root/trading-stack-smc',
        '/root/nkfx/infrastructure',
    ]
    
    for compose_dir in other_compose_dirs:
        compose_path = os.path.join(compose_dir, 'docker-compose.yml')
        if os.path.exists(compose_path):
            try:
                with open(compose_path) as f:
                    compose = yaml.safe_load(f)
                    for service_name, service_config in compose.get('services', {}).items():
                        # Use container_name if specified, otherwise service_name
                        container_name = service_config.get('container_name', service_name) if isinstance(service_config, dict) else service_name
                        if container_name not in seen_names:
                            discovered.append({
                                'name': container_name,
                                'type': 'docker',
                                'critical': is_critical_container(container_name),
                                'compose_path': compose_dir,
                                'auto_discovered': True,
                                'description': f'From: {compose_dir}'
                            })
                            seen_names.add(container_name)
            except Exception as e:
                log.debug(f"Error reading {compose_path}: {e}")
    
    return discovered


def is_critical_container(name: str) -> bool:
    """Determine if container is critical based on name."""
    return any(p in name.lower() for p in CRITICAL_PATTERNS)


def get_all_services(config: dict) -> List[dict]:
    """
    Get services from both config.yml AND auto-discovery.
    Auto-discovered services are merged with manual config.
    Manual config takes precedence for overrides.
    """
    services = []
    manual_names = set()
    
    # 1. Load manual config first (these take precedence)
    manual_services = config.get('services', [])
    for svc in manual_services:
        services.append(svc)
        manual_names.add(svc['name'])
    
    # 2. Auto-discover if enabled
    if config.get('settings', {}).get('auto_discovery', True):
        patterns = config.get('settings', {}).get('auto_patterns', DEFAULT_AUTO_PATTERNS)
        discovered = discover_containers(patterns)
        
        # 3. Merge - don't duplicate, manual config wins
        for d in discovered:
            if d['name'] not in manual_names:
                services.append(d)
                log.debug(f"Auto-discovered: {d['name']}")
    
    return services


# ============================================================
# CONFIG LOADING
# ============================================================

def load_config() -> dict:
    """Load configuration from YAML file."""
    if not CONFIG_FILE.exists():
        log.warning(f"Config file not found: {CONFIG_FILE}, using defaults")
        return {
            'settings': {
                'auto_discovery': True,
                'auto_restart': False,
                'alert_on_restart': True,
                'alert_on_recovery': True,
            },
            'telegram': {
                'bot_token': os.environ.get('TELEGRAM_BOT_TOKEN', ''),
                'chat_id': os.environ.get('TELEGRAM_CHAT_ID', ''),
            },
            'services': []
        }
    
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    """Load persistent state (restart counts, last check times)."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict):
    """Save state to file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ============================================================
# TELEGRAM ALERTS
# ============================================================

def send_telegram(config: dict, message: str, parse_mode: str = "HTML"):
    """Send a Telegram message."""
    token = config.get("telegram", {}).get("bot_token", "")
    chat_id = config.get("telegram", {}).get("chat_id", "")
    
    if not token or not chat_id:
        log.warning("Telegram not configured, skipping alert")
        return False
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error(f"Telegram send failed: {resp.text}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def alert_service_down(config: dict, service: dict, error: str, action: str, result: str):
    """Send alert for service down."""
    emoji = "🚨" if service.get("critical", False) else "⚠️"
    severity = "CRITICAL" if service.get("critical", False) else "WARNING"
    
    message = f"""
{emoji} <b>NKFX {severity}: {service['name']} DOWN</b>
━━━━━━━━━━━━━━━━━━━━━━
<b>Service:</b> {service.get('description', service['name'])}
<b>Status:</b> {error}
<b>Action:</b> {action}
<b>Result:</b> {result}
<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
"""
    send_telegram(config, message.strip())


def alert_service_recovered(config: dict, service: dict, downtime_mins: int):
    """Send alert when service recovers."""
    message = f"""
✅ <b>NKFX RECOVERED: {service['name']}</b>
━━━━━━━━━━━━━━━━━━━━━━
<b>Service:</b> {service.get('description', service['name'])}
<b>Downtime:</b> ~{downtime_mins} minutes
<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
"""
    send_telegram(config, message.strip())


# ============================================================
# HEALTH CHECKS
# ============================================================

def check_docker_container(name: str, pattern: str = None) -> Tuple[bool, str]:
    """
    Check if a Docker container is running.
    Returns (is_running, status_message)
    """
    search_name = pattern or name
    
    try:
        # First try exact name match
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=10
        )
        status = result.stdout.strip()
        
        # If not found, try pattern match
        if not status and pattern:
            result = subprocess.run(
                ["docker", "ps", "--filter", f"name={pattern}", "--format", "{{.Status}}"],
                capture_output=True, text=True, timeout=10
            )
            status = result.stdout.strip()
        
        # Also try without anchors for partial matches
        if not status:
            result = subprocess.run(
                ["docker", "ps", "--filter", f"name={search_name}", "--format", "{{.Status}}"],
                capture_output=True, text=True, timeout=10
            )
            status = result.stdout.strip()
        
        if status:
            # Check if it's actually running (not just existing)
            if "Up" in status:
                return True, status
            else:
                return False, f"Container exists but not running: {status}"
        else:
            return False, "Container not found"
            
    except subprocess.TimeoutExpired:
        return False, "Docker command timeout"
    except Exception as e:
        return False, f"Error: {e}"


def check_health_endpoint(url: str, timeout: int = 5) -> Tuple[bool, str]:
    """
    Check if a health endpoint returns 200.
    Returns (is_healthy, status_message)
    """
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            return True, "HTTP 200 OK"
        else:
            return False, f"HTTP {resp.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused"
    except requests.exceptions.Timeout:
        return False, "Request timeout"
    except Exception as e:
        return False, f"Error: {e}"


def restart_service(service: dict) -> Tuple[bool, str]:
    """
    Attempt to restart a service via docker compose.
    Returns (success, message)
    """
    compose_path = service.get("compose_path")
    
    if not compose_path:
        return False, "No compose_path configured"
    
    if not os.path.isdir(compose_path):
        return False, f"Compose path not found: {compose_path}"
    
    try:
        # Try docker compose (v2) first, fallback to docker-compose (v1)
        for cmd in ["docker compose", "docker-compose"]:
            result = subprocess.run(
                f"cd {compose_path} && {cmd} up -d {service['name']}",
                shell=True, capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                return True, "Restarted successfully"
        
        return False, f"Restart failed: {result.stderr[:200]}"
        
    except subprocess.TimeoutExpired:
        return False, "Restart timeout (120s)"
    except Exception as e:
        return False, f"Restart error: {e}"


# ============================================================
# MAIN HEALTH CHECK LOGIC
# ============================================================

def check_service(service: dict, config: dict, state: dict) -> dict:
    """
    Check a single service and handle alerts/restarts.
    Returns updated service state.
    """
    name = service["name"]
    service_state = state.get(name, {
        "last_status": "unknown",
        "last_check": None,
        "down_since": None,
        "restart_count_today": 0,
        "restart_count_total": 0
    })
    
    # Reset daily restart count if new day
    last_check = service_state.get("last_check")
    if last_check:
        try:
            last_date = datetime.fromisoformat(last_check).date()
            if last_date != datetime.now().date():
                service_state["restart_count_today"] = 0
        except:
            pass
    
    # Perform checks
    is_healthy = True
    error_msg = ""
    
    # Check 1: Docker container running
    if service.get("type") == "docker":
        pattern = service.get("container_pattern")
        running, status = check_docker_container(name, pattern)
        if not running:
            is_healthy = False
            error_msg = status
    
    # Check 2: Health endpoint (if container is running)
    if is_healthy and service.get("health_endpoint"):
        healthy, status = check_health_endpoint(service["health_endpoint"])
        if not healthy:
            is_healthy = False
            error_msg = f"Health check failed: {status}"
    
    # Handle status change
    was_healthy = service_state.get("last_status") == "healthy"
    
    if is_healthy:
        # Service is healthy
        if not was_healthy and service_state.get("down_since"):
            # Just recovered
            try:
                down_since = datetime.fromisoformat(service_state["down_since"])
                downtime = int((datetime.now() - down_since).total_seconds() / 60)
            except:
                downtime = 0
            
            if config.get("settings", {}).get("alert_on_recovery", True):
                alert_service_recovered(config, service, downtime)
            
            log.info(f"✅ {name} recovered after {downtime} minutes")
        
        service_state["last_status"] = "healthy"
        service_state["down_since"] = None
        
    else:
        # Service is down
        if was_healthy or service_state.get("last_status") == "unknown":
            # Just went down
            service_state["down_since"] = datetime.now().isoformat()
            log.warning(f"🚨 {name} is DOWN: {error_msg}")
        
        service_state["last_status"] = "down"
        
        # Attempt restart if enabled
        action = "Manual intervention needed"
        result = "❌ Auto-restart disabled"
        
        if config.get("settings", {}).get("auto_restart", False):
            action = "Attempting restart..."
            success, result_msg = restart_service(service)
            
            if success:
                result = "✅ Restarted successfully"
                service_state["restart_count_today"] += 1
                service_state["restart_count_total"] += 1
                log.info(f"✅ {name} restarted successfully")
            else:
                result = f"❌ {result_msg}"
                log.error(f"❌ {name} restart failed: {result_msg}")
        
        # Send alert
        if config.get("settings", {}).get("alert_on_restart", True):
            alert_service_down(config, service, error_msg, action, result)
    
    service_state["last_check"] = datetime.now().isoformat()
    return service_state


def run_health_check(config: dict):
    """Run health check on all services."""
    state = load_state()
    
    log.info("=" * 50)
    log.info("Starting health check...")
    
    # Get all services (manual + auto-discovered)
    services = get_all_services(config)
    healthy_count = 0
    down_count = 0
    
    log.info(f"Checking {len(services)} services...")
    
    for service in services:
        name = service["name"]
        service_state = check_service(service, config, state)
        state[name] = service_state
        
        if service_state["last_status"] == "healthy":
            healthy_count += 1
        else:
            down_count += 1
    
    state["last_full_check"] = datetime.now().isoformat()
    state["services_checked"] = len(services)
    save_state(state)
    
    log.info(f"Health check complete: {healthy_count} healthy, {down_count} down")


# ============================================================
# DAILY SUMMARY
# ============================================================

def send_daily_summary(config: dict):
    """Send daily health summary."""
    state = load_state()
    services = get_all_services(config)
    
    lines = ["📊 <b>NKFX Daily Health Report</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    
    total_restarts = 0
    critical_down = 0
    auto_discovered_count = 0
    
    for service in services:
        name = service["name"]
        svc_state = state.get(name, {})
        status = svc_state.get("last_status", "unknown")
        restarts = svc_state.get("restart_count_today", 0)
        total_restarts += restarts
        
        if service.get("auto_discovered"):
            auto_discovered_count += 1
        
        if status == "healthy":
            emoji = "✅"
            status_text = "Running"
        elif status == "down":
            emoji = "❌"
            status_text = "DOWN"
            if service.get("critical"):
                critical_down += 1
        else:
            emoji = "❓"
            status_text = "Unknown"
        
        line = f"{emoji} {name}: {status_text}"
        if restarts > 0:
            line += f" (restarted {restarts}x)"
        lines.append(line)
    
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"<b>Services:</b> {len(services)} ({auto_discovered_count} auto-discovered)")
    lines.append(f"<b>Total restarts today:</b> {total_restarts}")
    
    if critical_down > 0:
        lines.append(f"⚠️ <b>{critical_down} critical service(s) down!</b>")
    else:
        lines.append("✅ All critical services healthy")
    
    lines.append(f"\n<i>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}</i>")
    
    message = "\n".join(lines)
    send_telegram(config, message)
    log.info("Daily summary sent")


# ============================================================
# STATUS DISPLAY
# ============================================================

def show_status(config: dict):
    """Display current status of all services."""
    state = load_state()
    services = get_all_services(config)
    
    print("\n" + "=" * 70)
    print("NKFX SERVICE STATUS")
    print("=" * 70)
    
    auto_count = 0
    manual_count = 0
    
    for service in services:
        name = service["name"]
        svc_state = state.get(name, {})
        
        # Do a live check
        if service.get("type") == "docker":
            pattern = service.get("container_pattern")
            running, live_status = check_docker_container(name, pattern)
            live = "✅" if running else "❌"
        else:
            live = "?"
        
        restarts = svc_state.get("restart_count_today", 0)
        critical = "[CRIT]" if service.get("critical") else ""
        auto = "[AUTO]" if service.get("auto_discovered") else ""
        
        if service.get("auto_discovered"):
            auto_count += 1
        else:
            manual_count += 1
        
        print(f"{live} {name:30} | Restarts: {restarts:2} | {critical:6} {auto}")
    
    print("=" * 70)
    print(f"Total: {len(services)} services ({manual_count} manual, {auto_count} auto-discovered)")
    last_check = state.get("last_full_check", "Never")
    print(f"Last check: {last_check}")
    print()


def show_discovered(config: dict):
    """Show all auto-discovered containers."""
    patterns = config.get('settings', {}).get('auto_patterns', DEFAULT_AUTO_PATTERNS)
    discovered = discover_containers(patterns)
    
    print("\n" + "=" * 60)
    print("AUTO-DISCOVERED CONTAINERS")
    print("=" * 60)
    print(f"Patterns: {', '.join(patterns)}")
    print("-" * 60)
    
    for d in discovered:
        critical = "[CRITICAL]" if d.get("critical") else ""
        print(f"  {d['name']:30} {critical:10} {d.get('description', '')}")
    
    print("-" * 60)
    print(f"Total: {len(discovered)} containers")
    print()


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="NKFX Health Monitor")
    parser.add_argument("--daily", "--daily-summary", action="store_true", 
                        help="Send daily summary")
    parser.add_argument("--status", action="store_true",
                        help="Show current status")
    parser.add_argument("--discover", action="store_true",
                        help="Show auto-discovered containers")
    parser.add_argument("--test-alert", action="store_true",
                        help="Send test alert")
    args = parser.parse_args()
    
    config = load_config()
    
    if args.daily:
        send_daily_summary(config)
    elif args.status:
        show_status(config)
    elif args.discover:
        show_discovered(config)
    elif args.test_alert:
        send_telegram(config, "🔔 <b>NKFX Health Monitor Test</b>\n\nThis is a test alert. Monitoring is working correctly.")
        print("Test alert sent!")
    else:
        run_health_check(config)


if __name__ == "__main__":
    main()
