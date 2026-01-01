import socket
import threading
import json
import os
import ssl
import xbmc
import xbmcaddon
import xbmcgui
import requests
from urllib.parse import urlparse
import select
import concurrent.futures
import time
import re

import itertools

# --- DoH Implementation ---
ORIGINAL_GETADDRINFO = socket.getaddrinfo
DNS_CACHE = {}
DNS_LOCK = threading.Lock()

# Episode Cache
EPISODE_CACHE = {}
EPISODE_PATTERN = re.compile(r'/tv/(\d+)/season/(\d+)/episode/(\d+)$')

# IMDB Cache (Single Entry)
IMDB_CACHE = {}
IMDB_PATTERN = re.compile(r'imdb\.com/title/(tt\d+)/')

# 用户自定义IP映射 (从插件设置加载)
CUSTOM_IP_MAP = {}
HOSTS_MAP = {}
TARGET_DOMAINS = ['themoviedb.org', 'tmdb.org', 'fanart.tv', 'imdb.com', 'trakt.tv']
BUFFER_SIZE = 4096
DEFAULT_PORT = 56790
HOST = '127.0.0.1'

ADDON = xbmcaddon.Addon(id='metadata.tvshows.tmdb.cn.optimization')
CHAR_MAP = {}

def load_char_map():
    global CHAR_MAP
    try:
        addon_path = ADDON.getAddonInfo('path')
        # Handle potential encoding issues with path on Windows
        if isinstance(addon_path, bytes):
            addon_path = addon_path.decode('utf-8')
            
        map_path = os.path.join(addon_path, 'resources', 'char_map.json')
        if os.path.exists(map_path):
            with open(map_path, 'r', encoding='utf-8') as f:
                CHAR_MAP = json.load(f)
            xbmc.log(f'[TMDB TV Service] Loaded char_map.json with {len(CHAR_MAP)} entries', xbmc.LOGINFO)
        else:
            xbmc.log(f'[TMDB TV Service] char_map.json not found at {map_path}', xbmc.LOGWARNING)
    except Exception as e:
        xbmc.log(f'[TMDB TV Service] Failed to load char_map.json: {e}', xbmc.LOGERROR)

def get_pinyin_permutations(text):
    if not text:
        return ""
    
    # Convert each char to a list of possible initials
    char_initials = []
    for char in text:
        if char in CHAR_MAP:
            # Get all pinyin variations for this char
            pinyins = CHAR_MAP[char]
            # Extract initials and deduplicate preserving order
            initials = []
            seen = set()
            for p in pinyins:
                if p:
                    init = p[0].upper()
                    if init not in seen:
                        seen.add(init)
                        initials.append(init)
            
            if initials:
                char_initials.append(initials)
            else:
                if char.isalnum():
                    char_initials.append([char.upper()])
        else:
            if char.isalnum():
                char_initials.append([char.upper()])

    # Generate Cartesian product
    try:
        permutations = list(itertools.product(*char_initials))
        # Join them back into strings
        results = ["".join(p) for p in permutations]
        # Deduplicate preserving order
        seen_res = set()
        unique_results = []
        for r in results:
            if r not in seen_res:
                seen_res.add(r)
                unique_results.append(r)
        return "|".join(unique_results)
    except Exception as e:
        xbmc.log(f'[TMDB TV Service] Pinyin generation error: {e}', xbmc.LOGERROR)
        return text

def load_custom_ips():
    global CUSTOM_IP_MAP, TARGET_DOMAINS
    CUSTOM_IP_MAP = {}
    
    # Helper to extract domain from URL if needed
    def get_domain(url):
        if '://' in url:
            return urlparse(url).netloc
        return url.split('/')[0]

    tmdb_domain = ADDON.getSetting('tmdb_api_base_url') or 'api.tmdb.org'
    fanart_domain = ADDON.getSetting('fanart_base_url') or 'webservice.fanart.tv'
    trakt_domain = ADDON.getSetting('trakt_base_url') or 'trakt.tv'
    imdb_domain = ADDON.getSetting('imdb_base_url') or 'www.imdb.com'

    tmdb_host = get_domain(tmdb_domain)
    fanart_host = get_domain(fanart_domain)
    trakt_host = get_domain(trakt_domain)
    imdb_host = get_domain(imdb_domain)

    # Update TARGET_DOMAINS
    for d in [tmdb_host, fanart_host, trakt_host, imdb_host]:
        if d and d not in TARGET_DOMAINS:
            TARGET_DOMAINS.append(d)
    
    # Mapping setting ID to domain
    settings_map = {
        'dns_tmdb_api': tmdb_host,
        'dns_fanart_tv': fanart_host,
        'dns_imdb_www': imdb_host,
        'dns_trakt_tv': trakt_host
    }
    
    for setting_id, domain in settings_map.items():
        ip = ADDON.getSettingString(setting_id).strip()
        if ip and is_ip_address(ip):
            CUSTOM_IP_MAP[domain] = ip
            
    xbmc.log(f'[TMDB TV Service] Loaded {len(CUSTOM_IP_MAP)} custom IPs from settings', xbmc.LOGINFO)

def parse_hosts_file(path):
    mapping = {}
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        ip = parts[0]
                        if is_ip_address(ip):
                            for domain in parts[1:]:
                                mapping[domain] = ip
            xbmc.log(f'[TMDB TV Service] Loaded {len(mapping)} entries from {path}', xbmc.LOGINFO)
    except Exception as e:
        xbmc.log(f'[TMDB TV Service] Failed to read hosts file {path}: {e}', xbmc.LOGWARNING)
    return mapping

def load_hosts():
    global HOSTS_MAP
    HOSTS_MAP = {}
    
    # 1. System Hosts
    system_hosts = '/etc/hosts'
    if xbmc.getCondVisibility('System.Platform.Windows'):
        system_hosts = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32', 'drivers', 'etc', 'hosts')
    
    HOSTS_MAP.update(parse_hosts_file(system_hosts))

    # 2. Addon Userdata Hosts
    try:
        profile_dir = xbmc.translatePath(ADDON.getAddonInfo('profile'))
        if not os.path.exists(profile_dir):
            os.makedirs(profile_dir)
        user_hosts = os.path.join(profile_dir, 'hosts')
        HOSTS_MAP.update(parse_hosts_file(user_hosts))
    except:
        pass

def is_ip_address(host):
    try:
        socket.inet_aton(host)
        return True
    except:
        return ':' in host

def check_connectivity(ip, port=443, timeout=2.0, host=None):
    try:
        # Create a raw socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        
        # Wrap the socket with SSL
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        
        sock.connect((ip, port))
        
        # Perform SSL Handshake
        with context.wrap_socket(sock, server_hostname=host if host else ip) as ssock:
            pass
            
        xbmc.log(f'[TMDB TV Service] SSL Connectivity check succeeded for {ip}:{port}', xbmc.LOGINFO)
        return True
    except Exception as e:
        xbmc.log(f'[TMDB TV Service] SSL Connectivity check failed for {ip}:{port} Error: {e}', xbmc.LOGWARNING)
        return False

def doh_lookup(host):
    # 1. 检查 Hosts 文件 (系统 + 插件数据目录)
    if host in HOSTS_MAP:
        xbmc.log(f'[TMDB TV Service] Found in HOSTS file: {host} -> {HOSTS_MAP[host]}', xbmc.LOGINFO)
        return HOSTS_MAP[host]
    # 2. 检查自定义映射
    if host in CUSTOM_IP_MAP:
        ip = CUSTOM_IP_MAP[host]
        if ip and check_connectivity(ip, host=host):
            xbmc.log(f'[TMDB TV Service] Using Custom IP for {host} -> {ip}', xbmc.LOGINFO)
            return ip
        else:
            xbmc.log(f'[TMDB TV Service] Custom IP {ip} for {host} is unreachable, skipping...', xbmc.LOGWARNING)

    
    

    with DNS_LOCK:
        if host in DNS_CACHE:
            return DNS_CACHE[host]

    # DoH Providers List
    doh_providers = [
        # 1. Cloudflare (Global) - 1.1.1.1
        ("https://1.1.1.1/dns-query", "application/dns-json"),
        
        # 2. AliDNS (Alibaba Cloud) - Fallback
        ("https://223.5.5.5/resolve", "application/json"),
        ("https://223.6.6.6/resolve", "application/json"),
    ]

    for url, accept_header in doh_providers:
        try:
            # Direct request to IP to avoid recursion
            resp = requests.get(
                url,
                params={"name": host, "type": "A"},
                headers={"Accept": accept_header},
                timeout=2 # Short timeout for fast failover
            )
            if resp.status_code == 200:
                data = resp.json()
                if 'Answer' in data:
                    for answer in data['Answer']:
                        if answer['type'] == 1: # A Record
                            ip = answer['data']
                            with DNS_LOCK:
                                DNS_CACHE[host] = ip
                            xbmc.log(f'[TMDB TV Service] DoH Resolved {host} -> {ip} via {url}', xbmc.LOGINFO)
                            return ip
        except Exception as e:
            xbmc.log(f'[TMDB TV Service] DoH Lookup Failed via {url}: {e}', xbmc.LOGWARNING)
            continue
    
    return None

def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if is_ip_address(host):
        return ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
        
    # Intercept target domains
    if any(d in host for d in TARGET_DOMAINS):
        ip = doh_lookup(host)
        if ip:
            # Return IPv4 TCP address
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]
            
    return ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)

socket.getaddrinfo = patched_getaddrinfo
# --------------------------

# Configuration
DEFAULT_PORT = 56790
HOST = '127.0.0.1'
BUFFER_SIZE = 4096

class SessionManager:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get_session(self, url):
        try:
            domain = urlparse(url).netloc
        except:
            domain = "default"
        
        with self._lock:
            if domain not in self._sessions:
                s = requests.Session()
                # Configure session (e.g., headers, adapters)
                adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
                s.mount('http://', adapter)
                s.mount('https://', adapter)
                self._sessions[domain] = s
                xbmc.log(f'[TMDB TV Service] -----New session created for {domain} {url}', xbmc.LOGWARNING)
            return self._sessions[domain]

session_manager = SessionManager()

# Thread Pool Management
THREAD_POOL = None
POOL_LOCK = threading.Lock()
LAST_POOL_USE = 0
POOL_TIMEOUT = 20  # Seconds to keep pool alive

def get_thread_pool():
    global THREAD_POOL, LAST_POOL_USE
    with POOL_LOCK:
        LAST_POOL_USE = time.time()
        if THREAD_POOL is None:
            xbmc.log('[TMDB TV Service] Creating new ThreadPoolExecutor', xbmc.LOGDEBUG)
            THREAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        return THREAD_POOL

def execute_request(request):
    url = request.get('url')
    params = request.get('params')
    headers = request.get('headers', {})
    
    if not url:
        return {'error': 'No URL provided'}

    session = session_manager.get_session(url)
    
    try:
        resp = session.get(url, params=params, headers=headers, timeout=30)
        xbmc.log(f'[TMDB TV Service] -----Fetched URL: {resp.url} Status: {resp.status_code}', xbmc.LOGDEBUG)
        resp.raise_for_status()
        
        result = {
            'status': resp.status_code,
            'text': resp.text,
            'json': None
        }
        try:
            result['json'] = resp.json()
        except:
            pass
        return result
    except Exception as e:
        return {'error': str(e)}

def get_episode_key(tv_id, season, episode):
    return f"{tv_id}_{season}_{episode}"

def process_single_request_with_cache(request):
    url = request.get('url', '')
    params = request.get('params', {})
    
    # Check if it is an episode request
    parsed = urlparse(url)
    match = EPISODE_PATTERN.search(parsed.path)
    
    if match:
        tv_id, season, episode = match.groups()
        lang = params.get('language', 'en-US') # Default to en-US if missing
        
        episode_key = get_episode_key(tv_id, season, episode)
        
        # Check all language caches for staleness
        # If the requested episode is missing in a language cache, it means that cache is stale/out of range
        for cached_lang in list(EPISODE_CACHE.keys()):
            if EPISODE_CACHE[cached_lang] and episode_key not in EPISODE_CACHE[cached_lang]:
                xbmc.log(f'[TMDB TV Service] Clearing stale cache for language: {cached_lang} (Missing {episode_key})', xbmc.LOGDEBUG)
                EPISODE_CACHE[cached_lang].clear()
        
        # Ensure the requested language cache exists
        if lang not in EPISODE_CACHE:
            EPISODE_CACHE[lang] = {}
            
        if episode_key in EPISODE_CACHE[lang]:
            xbmc.log(f'[TMDB TV Service] Cache HIT for {episode_key} ({lang})', xbmc.LOGDEBUG)
            return EPISODE_CACHE[lang][episode_key]
        
        # Cache Miss - Trigger Batch Fetch
        xbmc.log(f'[TMDB TV Service] Cache MISS for {episode_key} ({lang}) - Triggering Batch Fetch', xbmc.LOGDEBUG)
        
        # Prepare batch requests (Current + 7)
        batch_requests = []
        
        start_ep = int(episode)
        for i in range(8):
            curr_ep = start_ep + i
            # Reconstruct URL: replace the last number in the path
            new_url = re.sub(r'/episode/\d+$', f'/episode/{curr_ep}', url)
            
            req = request.copy()
            req['url'] = new_url
            # Params are same
            batch_requests.append(req)
            
        # Execute batch
        results = []
        # Use the global thread pool
        executor = get_thread_pool()
        results = list(executor.map(execute_request, batch_requests))
             
        # Cache results
        target_result = None
        
        for i, res in enumerate(results):
            if res.get('error'): continue
            
            # Check if valid response (e.g. not 404)
            if res.get('status') == 200:
                curr_ep = start_ep + i
                k = get_episode_key(tv_id, season, str(curr_ep))
                EPISODE_CACHE[lang][k] = res
                
                if i == 0:
                    target_result = res
        
        if target_result:
            return target_result
        
        # If the specific requested episode failed (e.g. 404), return the result of that specific request
        return results[0]

    # Check if it is an IMDB request
    imdb_match = IMDB_PATTERN.search(url)
    if imdb_match:
        imdb_id = imdb_match.group(1)
        
        if imdb_id in IMDB_CACHE:
            xbmc.log(f'[TMDB TV Service] IMDB Cache HIT for {imdb_id}', xbmc.LOGDEBUG)
            return IMDB_CACHE[imdb_id]
            
        xbmc.log(f'[TMDB TV Service] IMDB Cache MISS for {imdb_id} - Clearing Cache & Fetching', xbmc.LOGDEBUG)
        
        # Clear IMDB cache on miss (Keep only 1 entry)
        IMDB_CACHE.clear()
        
        # Execute request
        result = execute_request(request)
        
        if result.get('status') == 200:
            IMDB_CACHE[imdb_id] = result
            
        return result

    else:
        return execute_request(request)

def handle_client(conn, addr):
    try:
        data = b""
        while True:
            chunk = conn.recv(BUFFER_SIZE)
            if not chunk:
                break
            data += chunk
            try:
                json.loads(data)
                break 
            except:
                continue
        
        if not data:
            return

        payload = json.loads(data)
        
        dns_settings = None
        requests_list = []
        
        # Protocol V2: Dict with 'requests' and optional 'dns_settings'
        if isinstance(payload, dict) and 'requests' in payload:
            requests_list = payload['requests']
            xbmc.log(f'[TMDB TV Service] ----Request (V2) with {len(requests_list)} requests, {requests_list[0]}', xbmc.LOGDEBUG)
            dns_settings = payload.get('dns_settings')
        # Protocol V3: Pinyin Request
        elif isinstance(payload, dict) and 'pinyin' in payload:
            text = payload['pinyin']
            try:
                result = get_pinyin_permutations(text)
                conn.sendall(json.dumps({'result': result}).encode('utf-8'))
            except Exception as e:
                conn.sendall(json.dumps({'error': str(e)}).encode('utf-8'))
            return
        # Protocol V1: List (Batch)
        elif isinstance(payload, list):
            xbmc.log(f'[TMDB TV Service] ----Request (V1) with {len(payload)} requests, {payload[0]}', xbmc.LOGDEBUG)
            requests_list = payload
        # Protocol V1: Dict (Single)
        else:
            xbmc.log(f'[TMDB TV Service] ----Request (V1) Single request, {payload}', xbmc.LOGDEBUG)
            requests_list = [payload]

        # Log simplified URLs for debugging
        log_items = []
        for itm in requests_list:
            u = urlparse(itm.get('url', ''))
            # Keep path, truncate if too long
            path = u.path
            if len(path) > 30:
                path = path[:15] + '...' + path[-10:]
            log_items.append(f"{u.netloc}{path}")
        
        xbmc.log(f'[TMDB TV Service] ----Request ({len(requests_list)}): {log_items} | DNS Override: {bool(dns_settings)}', xbmc.LOGDEBUG)
        
        if dns_settings:
            changes = {}
            for k, v in dns_settings.items():
                # If value is empty, we want to remove it from CUSTOM_IP_MAP if it exists
                if not v:
                    if k in CUSTOM_IP_MAP:
                        del CUSTOM_IP_MAP[k]
                        changes[k] = "<REMOVED>"
                # If value is not empty, update if different
                elif CUSTOM_IP_MAP.get(k) != v:
                    CUSTOM_IP_MAP[k] = v
                    changes[k] = v
            
            if changes:
                xbmc.log(f'[TMDB TV Service] Updated Global Custom IPs: {changes}', xbmc.LOGINFO)

        if len(requests_list) == 1:
            # Only use cache logic for single requests
            results = [process_single_request_with_cache(requests_list[0])]
        else:
            # For batch requests, execute directly (no cache logic applied)
            executor = get_thread_pool()
            results = list(executor.map(execute_request, requests_list))
            
        # If original payload was single dict (V1), return single result
        if isinstance(payload, dict) and 'requests' not in payload:
             conn.sendall(json.dumps(results[0]).encode('utf-8'))
        else:
             conn.sendall(json.dumps(results).encode('utf-8'))

    except Exception as e:
        xbmc.log(f'[TMDB TV Service] Client Error: {e}', xbmc.LOGERROR)
    finally:
        conn.close()

def start_server(monitor):
    global THREAD_POOL
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    port = DEFAULT_PORT
    bound = False
    
    try:
        # Try default port first
        try:
            server.bind((HOST, port))
            bound = True
        except OSError:
            # Fallback to random port
            xbmc.log(f'[TMDB TV Service] Port {port} in use, trying random port', xbmc.LOGWARNING)
            server.bind((HOST, 0))
            bound = True
            
        if bound:
            port = server.getsockname()[1]
            server.listen(5)
            server.setblocking(False) # Non-blocking for select
            
            # Announce port via Window Property
            window = xbmcgui.Window(10000)
            window.setProperty('TMDB_TV_OPTIMIZATION_SERVICE_PORT', str(port))
            
            xbmc.log(f'[TMDB TV Service] Daemon started on {HOST}:{port}', xbmc.LOGINFO)
            
            last_activity = time.time()
            IDLE_TIMEOUT = 20 # seconds

            while not monitor.abortRequested():
                # Use select to wait for connections or timeout to check abortRequested
                readable, _, _ = select.select([server], [], [], 1.0)
                
                if server in readable:
                    last_activity = time.time()
                    conn, addr = server.accept()
                    conn.setblocking(True) # Ensure blocking mode for the thread handler
                    # Handle in a thread to not block other requests
                    t = threading.Thread(target=handle_client, args=(conn, addr))
                    t.daemon = True
                    t.start()
                elif time.time() - last_activity > IDLE_TIMEOUT:
                    xbmc.log('[TMDB TV Service] No activity for 20s, shutting down daemon', xbmc.LOGINFO)
                    break
                
                # Check pool cleanup
                with POOL_LOCK:
                    if THREAD_POOL and (time.time() - LAST_POOL_USE > POOL_TIMEOUT):
                        xbmc.log('[TMDB TV Service] Shutting down idle ThreadPoolExecutor', xbmc.LOGINFO)
                        THREAD_POOL.shutdown(wait=False)
                        THREAD_POOL = None
                    
    except Exception as e:
        xbmc.log(f'[TMDB TV Service] Server Error: {e}', xbmc.LOGERROR)
    finally:
        server.close()
        # Clean up property
        xbmcgui.Window(10000).clearProperty('TMDB_TV_OPTIMIZATION_SERVICE_PORT')
        
        # Ensure ThreadPool is shut down
        with POOL_LOCK:
            if THREAD_POOL:
                xbmc.log('[TMDB TV Service] Shutting down ThreadPoolExecutor on exit', xbmc.LOGINFO)
                THREAD_POOL.shutdown(wait=False)
                THREAD_POOL = None

        xbmc.log('[TMDB TV Service] Daemon stopped', xbmc.LOGINFO)

class SettingsMonitor(xbmc.Monitor):
    def onSettingsChanged(self):
        xbmc.log('[TMDB TV Service] Settings changed, reloading IPs and clearing cache...', xbmc.LOGINFO)
        load_custom_ips()
        load_hosts()
        EPISODE_CACHE.clear()
        IMDB_CACHE.clear()

if __name__ == '__main__':
    monitor = SettingsMonitor()
    load_char_map() # Load pinyin map
    load_custom_ips()
    load_hosts() # Load hosts on startup
    start_server(monitor)
