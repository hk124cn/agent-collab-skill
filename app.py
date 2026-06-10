#!/usr/bin/env python3
"""
Agent Collaboration Platform
多Agent协作平台 - REST API + SSE实时推送 + Web界面
"""
import os
import json
import datetime
import logging
import threading
import time
from zoneinfo import ZoneInfo
import gevent
from gevent import monkey
monkey.patch_all()

from flask import Flask, request, jsonify, render_template_string, Response, stream_with_context
from functools import wraps
from queue import Queue
from typing import Set

app = Flask(__name__)

# 时区设置：中国时间 (Asia/Shanghai)
CST = ZoneInfo("Asia/Shanghai")

def now_cst():
    """获取当前中国时间 (Asia/Shanghai)"""
    return datetime.datetime.now(CST)

# 日志配置
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'login.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ========== 配置 ==========
app.config['JSON_AS_ASCII'] = False
app.config['JSON_SORT_KEYS'] = False

# 人类用户配置
HUMAN_USERS = {
    'daqin': {'name': '大秦', 'password': 'daqin123', 'is_admin': True},
    'yihao': {'name': '一号', 'password': 'yihao123', 'is_admin': False},
    'xiaobai': {'name': '小白', 'password': 'xiaobai123', 'is_admin': False},
}

# 用户访问密码 (旧版兼容，可移除)
USER_PASSWORD = os.environ.get('COLLAB_PASSWORD', 'admin129')

# 开放端口 (0-65535，建议3000-9000之间)
PORT = int(os.environ.get('COLLAB_PORT', '3847'))

# Cookie 配置
HUMAN_COOKIE_NAME = 'human_user'  # 记录当前登录的人类用户名
AGENT_COOKIE_NAME = 'agent_user'   # 记录当前登录的Agent身份

# 防暴力破解
import time
_login_attempts = {}  # {"ip": [timestamp1, timestamp2, ...]}

def check_rate_limit(ip):
    """简单限流：同一IP 60秒内最多5次尝试"""
    # 测试模式绕过限流
    if os.environ.get('COLLAB_TEST_MODE') == '1':
        return True
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts.get(ip, []) if now - t < 60]

    if len(_login_attempts.get(ip, [])) >= 5:
        return False

    _login_attempts[ip] = _login_attempts.get(ip, []) + [now]
    return True


# ========== SSE 广播机制 ==========
class SSEBroadcaster:
    """SSE消息广播器 - gevent 安全的客户端管理和消息推送"""
    
    def __init__(self):
        from gevent.lock import RLock as Lock
        from gevent.queue import Queue
        
        self._clients = set()
        self._lock = Lock()
        self._running = False
        self._thread = None
    
    def register(self):
        """注册新的 SSE 客户端连接"""
        queue = Queue()
        with self._lock:
            self._clients.add(queue)
            logger.info(f"[SSE] 新客户端连接，当前连接数：{len(self._clients)}")
        return queue
    
    def unregister(self, queue):
        """注销 SSE 客户端连接"""
        with self._lock:
            if queue in self._clients:
                self._clients.remove(queue)
                logger.info(f"[SSE] 客户端断开，当前连接数：{len(self._clients)}")
    
    def broadcast(self, event="message", data=None):
        """向所有连接的客户端广播消息"""
        if data is None:
            data = {}
        
        message = {
            "event": event,
            "data": data,
            "timestamp": now_cst().isoformat()
        }
        
        serialized = f"event: {event}\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
        
        with self._lock:
            dead_clients = set()
            logger.info(f"[SSE] 广播到 {len(self._clients)} 个客户端")
            for queue in self._clients:
                try:
                    queue.put(serialized)
                    logger.debug("[SSE] 消息已放入队列")
                except Exception as e:
                    logger.warning(f"[SSE] 队列放入失败：{e}")
                    dead_clients.add(queue)
            
            # 清理死连接
            for dead in dead_clients:
                self._clients.discard(dead)
        
        if dead_clients:
            logger.info(f"[SSE] 清理 {len(dead_clients)} 个死连接")
    
    def start_background_thread(self):
        """启动后台线程用于处理定时心跳等（可选）"""
        self._running = True
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """停止广播器"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
    
    def _heartbeat_loop(self):
        """后台心跳循环 - 保持连接活跃"""
        import gevent
        while self._running:
            gevent.sleep(15)  # 每15秒发送心跳，防止代理/负载均衡器断连
            if self._running:
                self.broadcast("heartbeat", {"status": "alive"})


# 全局广播器实例
broadcaster = SSEBroadcaster()
broadcaster.start_background_thread()  # 启动心跳线程（55秒间隔，防中间件断连）


# ========== 工具函数 ==========
def load_json(path, default):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_today():
    return now_cst().strftime('%Y-%m-%d')

def get_messages_file(date=None):
    if date is None:
        date = get_today()
    return os.path.join(BASE_DIR, 'messages', f'{date}.json')


# ========== 认证装饰器 ==========
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing auth token'}), 401
        
        token = auth_header[7:]
        
        # 验证token
        registry = load_json(os.path.join(BASE_DIR, 'agents', 'registry.json'), {'agents': []})
        valid_keys = [a['api_key'] for a in registry.get('agents', []) if a.get('active')]
        
        if token not in valid_keys:
            return jsonify({'error': 'Invalid auth token'}), 403
        
        return f(*args, **kwargs)
    return decorated


# ========== SSE 端点 ==========
@app.route('/api/stream')
def sse_stream():
    """
    SSE (Server-Sent Events) 实时消息推送端点
    
    客户端通过 EventSource 连接此端点，实时接收新消息通知
    """
    logger.info("[SSE] sse_stream() 被调用")
    
    # 注册客户端
    queue = broadcaster.register()
    logger.info(f"[SSE] 客户端注册成功，队列：{queue}")
    
    def generate():
        logger.info("[SSE] generate() 开始")
        try:
            while True:
                try:
                    # 从队列获取消息（阻塞等待，15秒超时）
                    message = queue.get(timeout=15)
                    logger.info(f"[SSE] 收到队列消息，yielding...")
                    yield message
                except Exception:
                    # 超时或空队列 — 发送 keep-alive 注释防止代理断连
                    logger.debug("[SSE] 发送 keep-alive")
                    yield ": keepalive\n\n"
        except GeneratorExit:
            logger.info("[SSE] 客户端断开 (GeneratorExit)")
        except Exception as e:
            logger.debug(f"[SSE] 客户端断开：{e}")
        finally:
            # 注销客户端
            logger.info("[SSE] 注销客户端")
            broadcaster.unregister(queue)
    
    response = Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Content-Type': 'text/event-stream; charset=utf-8',
            'X-Accel-Buffering': 'no',
        }
    )
    logger.info("[SSE] Response 返回")
    return response


# ========== API 路由 ==========

# 用于验证用户访问的密码
HUMAN_COOKIE_NAME = 'human_user'
AGENT_COOKIE_NAME = 'agent_user'

def check_human_auth():
    """检查人类用户是否已登录"""
    username = request.cookies.get(HUMAN_COOKIE_NAME)
    return username in HUMAN_USERS

def check_agent_auth():
    """检查Agent是否已通过API认证"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return False
    token = auth_header[7:]
    registry = load_json(os.path.join(BASE_DIR, 'agents', 'registry.json'), {'agents': []})
    for a in registry.get('agents', []):
        if a.get('api_key') == token and a.get('active'):
            return True
    return False

def get_current_user():
    """获取当前用户信息（人类或Agent）"""
    # 优先检查人类用户
    username = request.cookies.get(HUMAN_COOKIE_NAME)
    if username in HUMAN_USERS:
        user = HUMAN_USERS[username]
        return {
            'type': 'human',
            'id': username,
            'name': user['name'],
            'is_admin': user['is_admin']
        }
    
    # 检查Agent
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        registry = load_json(os.path.join(BASE_DIR, 'agents', 'registry.json'), {'agents': []})
        for a in registry.get('agents', []):
            if a.get('api_key') == token and a.get('active'):
                return {
                    'type': 'agent',
                    'id': a['id'],
                    'name': a['name'],
                    'is_admin': a['id'] == 'guwen'  # 只有顾问是管理员
                }
    
    return None

def require_auth(f):
    """认证装饰器 - 支持人类用户和Agent"""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if user is None:
            return jsonify({'error': '未认证'}), 401
        request.current_user = user  # 将用户信息附加到request
        return f(*args, **kwargs)
    return decorated
@app.route('/api/auth', methods=['POST'])
def user_login():
    """用户登录验证 - 支持人类用户和Agent"""
    # 先检查限流
    client_ip = request.remote_addr
    if not check_rate_limit(client_ip):
        logger.warning(f"[登录] 限流阻止 - IP: {client_ip}")
        return jsonify({'error': 'Too many attempts, try again later'}), 429
    
    # 检查 JSON 数据
    data = request.get_json()
    if not data:
        logger.warning(f"[登录] 缺少请求数据 from {client_ip}")
        return jsonify({'error': '请求数据格式错误'}), 400
    
    login_type = data.get('type', 'human')  # 'human' 或 'agent'
    
    if login_type == 'human':
        # 人类用户登录 - 严格验证
        username = data.get('username', '')
        pwd = data.get('password', '')
        
        # 输入验证
        if not username:
            logger.warning(f"[登录] 缺少用户名 from {client_ip}")
            return jsonify({'error': '请输入用户名'}), 400
        if not pwd:
            logger.warning(f"[登录] 缺少密码 from {client_ip}")
            return jsonify({'error': '请输入密码'}), 400
        
        # 检查用户是否存在
        user = HUMAN_USERS.get(username)
        if not user:
            logger.warning(f"[登录] 未知用户: {username} from {client_ip}")
            return jsonify({'error': '用户名不存在'}), 401
        if user['password'] != pwd:
            logger.warning(f"[登录] 密码错误 - {username} from {client_ip}")
            return jsonify({'error': '密码错误'}), 401
        
        # 登录成功
        logger.info(f"[登录] 人类用户成功 - {username} from {client_ip}")
        resp = jsonify({'success': True, 'type': 'human', 'name': user['name'], 'is_admin': user['is_admin']})
        resp.set_cookie(HUMAN_COOKIE_NAME, username, max_age=86400*7)  # 7天
        return resp
    
    elif login_type == 'agent':
        # Agent API 登录 - 严格验证
        agent_key = data.get('agent_key', '')
        if not agent_key:
            logger.warning(f"[登录] 缺少Agent密钥 from {client_ip}")
            return jsonify({'error': '请输入Agent密钥'}), 400
        
        registry = load_json(os.path.join(BASE_DIR, 'agents', 'registry.json'), {'agents': []})
        
        for a in registry.get('agents', []):
            if a.get('api_key') == agent_key and a.get('active'):
                logger.info(f"[登录] Agent 成功 - {a['name']} from {client_ip}")
                resp = jsonify({'success': True, 'type': 'agent', 'name': a['name'], 'id': a['id']})
                resp.set_cookie(AGENT_COOKIE_NAME, a['id'], max_age=86400*7)
                return resp
        
        logger.warning(f"[登录] Agent 失败 - 未知密钥 from {client_ip}")
        return jsonify({'error': '无效的Agent密钥'}), 401
    
    else:
        logger.warning(f"[登录] 未知登录类型: {login_type} from {client_ip}")
        return jsonify({'error': '未知的登录类型，请选择"人类用户"或"AI Agent"'}), 400


@app.route('/api/auth', methods=['GET'])
def user_status():
    """检查登录状态"""
    user = get_current_user()
    if user:
        return jsonify({'authenticated': True, 'type': user['type'], 'name': user['name'], 'is_admin': user['is_admin']})
    return jsonify({'authenticated': False})


@app.route('/api/agent/login', methods=['POST'])
def agent_login():
    """
    Agent 服务器端登录（无浏览器用）
    直接用 Authorization Bearer 更简单，这是备用方式。

    请求体: {"api_key": "agent_xxx_secret_key_2025"}
    返回: {"success": true, "agent": {...}, "token": "<api_key>"}

    注意: 返回的 token 就是 api_key 本身，客户端需要自行保存，
    后续请求带上 Authorization: Bearer <token> 即可。
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': '请求数据格式错误'}), 400

    api_key = data.get('api_key', '')
    if not api_key:
        return jsonify({'error': 'api_key 不能为空'}), 400

    registry = load_json(os.path.join(BASE_DIR, 'agents', 'registry.json'), {'agents': []})
    for a in registry.get('agents', []):
        if a.get('api_key') == api_key and a.get('active'):
            logger.info(f"[Agent登录] {a['name']} ({a['id']}) 登录成功 from {request.remote_addr}")
            return jsonify({
                'success': True,
                'agent': {
                    'id': a['id'],
                    'name': a['name'],
                    'role': a.get('role', ''),
                    'is_admin': a['id'] == 'guwen'
                },
                'token': api_key
            })

    logger.warning(f"[Agent登录] 无效密钥 from {request.remote_addr}")
    return jsonify({'error': '无效的 API Key'}), 401


@app.route('/api/logout', methods=['POST'])
def user_logout():
    """登出"""
    resp = jsonify({'success': True})
    resp.set_cookie(HUMAN_COOKIE_NAME, '', max_age=0)
    resp.set_cookie(AGENT_COOKIE_NAME, '', max_age=0)
    logger.info(f"[登出] 用户登出 from {request.remote_addr}")
    return resp


@app.route('/api/messages', methods=['GET'])
def get_messages():
    """获取消息列表"""
    date = request.args.get('date')  # None = 所有日期
    topic = request.args.get('topic')
    query = request.args.get('q', '').strip()  # 搜索关键词
    before = request.args.get('before', '').strip()  # 分页 cursor：取此时间戳之前的消息
    limit = request.args.get('limit', 50, type=int)

    msg_file = get_messages_file(date) if date else None
    if msg_file:
        messages = load_json(msg_file, [])
    else:
        # 读所有日期的消息文件
        messages_dir = os.path.join(BASE_DIR, 'messages')
        messages = []
        if os.path.exists(messages_dir):
            for fname in sorted(os.listdir(messages_dir), reverse=True):
                if fname.endswith('.json'):
                    messages.extend(load_json(os.path.join(messages_dir, fname), []))

    if topic:
        messages = [m for m in messages if m.get('topic') == topic]

    # 搜索过滤
    if query:
        q_lower = query.lower()
        messages = [m for m in messages if q_lower in m.get('content', '').lower()
                    or q_lower in m.get('author_name', '').lower()]
    
    # 将回复关联到原消息
    msg_dict = {m['id']: m for m in messages}
    for msg in messages:
        msg['replies'] = []
    
    # 找出回复并挂载到原消息下
    for msg in messages:
        reply_to = msg.get('reply_to')
        if reply_to and reply_to in msg_dict:
            if 'replies' not in msg_dict[reply_to]:
                msg_dict[reply_to]['replies'] = []
            msg_dict[reply_to]['replies'].append(msg)
    
    # 只显示主帖子（没有reply_to的），按时间倒序
    main_messages = [m for m in messages if not m.get('reply_to')]
    
    # 对每个消息的replies按时间排序
    for msg in main_messages:
        if msg.get('replies'):
            msg['replies'] = sorted(msg['replies'], key=lambda x: x.get('timestamp', ''))
    
    main_messages.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    main_messages.sort(key=lambda x: not x.get('pinned', False))  # 置顶排前

    # 分页：取比 cursor 更早的消息
    if before:
        before_idx = next((i for i, m in enumerate(main_messages) if m.get('timestamp', '') < before), len(main_messages))
        # 置顶消息始终包含在第一页；后续页只返回非置顶的
        pinned = [m for m in main_messages if m.get('pinned')]
        unpinned_before = [m for m in main_messages if not m.get('pinned') and m.get('timestamp', '') < before]
        main_messages = pinned + unpinned_before
        main_messages = main_messages[:limit]
    else:
        main_messages = main_messages[:limit]
    return jsonify(main_messages)


@app.route('/api/messages', methods=['POST'])
@require_auth
def post_message():
    """发布消息 - 同时触发SSE广播"""
    data = request.get_json()
    
    # 验证必要字段
    if not data.get('content'):
        return jsonify({'error': 'content is required'}), 400
    
    # 获取当前用户
    user = request.current_user
    
    # 支持回复功能
    reply_to = data.get('reply_to')  # 要回复的消息ID
    
    message = {
        'id': f"msg_{now_cst().timestamp()}",
        'author_type': user['type'],  # 'human' 或 'agent'
        'author_id': user['id'],      # 用户名或agent_id
        'author_name': user['name'],  # 显示名称
        'type': data.get('type', 'chat'),
        'topic': data.get('topic', 'share'),
        'content': data['content'],
        'reply_to': reply_to,
        'timestamp': now_cst().isoformat()
    }
    
    # 保存
    msg_file = get_messages_file()
    messages = load_json(msg_file, [])
    messages.append(message)
    save_json(msg_file, messages)
    
    # ========== SSE 广播新消息 ==========
    broadcaster.broadcast(
        event="new_message",
        data={
            "message": message,
            "action": "create"
        }
    )
    logger.info(f"[SSE] 广播新消息: {message['id']} from {message['author_name']}")
    
    return jsonify({'success': True, 'message': message})


@app.route('/api/messages/<msg_id>', methods=['DELETE'])
@require_auth
def delete_message(msg_id):
    """删除消息 - 权限检查：管理员可删任何，用户可删自己的"""
    user = request.current_user
    
    # 查找消息所在文件
    messages_dir = os.path.join(BASE_DIR, 'messages')
    if not os.path.exists(messages_dir):
        return jsonify({'error': '消息不存在'}), 404
    
    # 先找到消息，检查权限
    msg_found = None
    msg_file_path = None
    for fname in os.listdir(messages_dir):
        if fname.endswith('.json'):
            fpath = os.path.join(messages_dir, fname)
            messages = load_json(fpath, [])
            for m in messages:
                if m.get('id') == msg_id:
                    msg_found = m
                    msg_file_path = fpath
                    break
            if msg_found:
                break
    
    if not msg_found:
        return jsonify({'error': '消息不存在'}), 404
    
    # 权限检查
    can_delete = False
    
    # 管理员可以删除任何消息
    if user['is_admin']:
        can_delete = True
        logger.info(f"[删除] 管理员 {user['name']} 删除消息 {msg_id}")
    # 用户只能删除自己发的消息
    elif (msg_found.get('author_type') == user['type'] and 
          msg_found.get('author_id') == user['id']):
        can_delete = True
        logger.info(f"[删除] 用户 {user['name']} 删除自己的消息 {msg_id}")
    
    if not can_delete:
        logger.warning(f"[删除] 拒绝 - {user['name']} 无权删除消息 {msg_id}")
        return jsonify({'error': '无权删除此消息'}), 403
    
    # 执行删除（级联删除所有回复）
    messages = load_json(msg_file_path, [])
    # 找出所有 reply_to == msg_id 的回复 ID
    cascaded_ids = [m['id'] for m in messages if m.get('reply_to') == msg_id]
    if cascaded_ids:
        logger.info(f"[删除] 级联删除 {len(cascaded_ids)} 条回复: {cascaded_ids}")
    messages = [m for m in messages if m.get('id') != msg_id and m.get('reply_to') != msg_id]
    save_json(msg_file_path, messages)

    # 广播删除通知（含级联 ID）
    broadcaster.broadcast(
        event="delete_message",
        data={"message_id": msg_id, "cascaded_ids": cascaded_ids, "action": "delete"}
    )

    return jsonify({'success': True, 'cascaded': len(cascaded_ids)})


@app.route('/api/messages/<msg_id>/react', methods=['POST'])
@require_auth
def react_message(msg_id):
    """对消息添加/切换 emoji 反应"""
    data = request.get_json()
    emoji = data.get('emoji', '')
    if not emoji:
        return jsonify({'error': 'emoji is required'}), 400

    user = request.current_user
    user_key = f"{user['type']}:{user['id']}"

    # 查找消息
    messages_dir = os.path.join(BASE_DIR, 'messages')
    for fname in os.listdir(messages_dir):
        if not fname.endswith('.json'):
            continue
        fpath = os.path.join(messages_dir, fname)
        messages = load_json(fpath, [])
        for m in messages:
            if m.get('id') == msg_id:
                reactions = m.setdefault('reactions', {})
                users = reactions.setdefault(emoji, [])
                if user_key in users:
                    users.remove(user_key)  # 取消反应
                else:
                    users.append(user_key)  # 添加反应
                save_json(fpath, messages)
                return jsonify({'success': True, 'reactions': reactions})

    return jsonify({'error': '消息不存在'}), 404


@app.route('/api/messages/<msg_id>/like', methods=['POST'])
@require_auth
def like_message(msg_id):
    """点赞/取消点赞"""
    user = request.current_user
    user_key = f"{user['type']}:{user['id']}"

    messages_dir = os.path.join(BASE_DIR, 'messages')
    for fname in os.listdir(messages_dir):
        if not fname.endswith('.json'):
            continue
        fpath = os.path.join(messages_dir, fname)
        messages = load_json(fpath, [])
        for m in messages:
            if m.get('id') == msg_id:
                likes = m.setdefault('likes', [])
                if user_key in likes:
                    likes.remove(user_key)
                else:
                    likes.append(user_key)
                save_json(fpath, messages)
                return jsonify({'success': True, 'likes': len(likes), 'liked': user_key in likes})

    return jsonify({'error': '消息不存在'}), 404


@app.route('/api/messages/<msg_id>/pin', methods=['PUT'])
@require_auth
def pin_message(msg_id):
    """置顶/取消置顶消息（仅管理员）"""
    user = request.current_user
    if not user.get('is_admin'):
        return jsonify({'error': '仅管理员可置顶'}), 403

    messages_dir = os.path.join(BASE_DIR, 'messages')
    for fname in os.listdir(messages_dir):
        if not fname.endswith('.json'):
            continue
        fpath = os.path.join(messages_dir, fname)
        messages = load_json(fpath, [])
        for m in messages:
            if m.get('id') == msg_id:
                m['pinned'] = not m.get('pinned', False)
                save_json(fpath, messages)
                return jsonify({'success': True, 'pinned': m['pinned']})

    return jsonify({'error': '消息不存在'}), 404


@app.route('/api/topics', methods=['GET'])
def get_topics():
    """获取话题列表"""
    topics = load_json(os.path.join(BASE_DIR, 'topics', 'topics.json'), [])
    return jsonify(topics)


@app.route('/api/dates', methods=['GET'])
def get_dates():
    """获取有消息的日期列表"""
    messages_dir = os.path.join(BASE_DIR, 'messages')
    if not os.path.exists(messages_dir):
        return jsonify([])
    
    files = [f.replace('.json', '') for f in os.listdir(messages_dir) if f.endswith('.json')]
    return jsonify(sorted(files, reverse=True))


# ========== 用户管理 API ==========

@app.route('/api/login-logs', methods=['GET'])
@require_auth
def get_login_logs():
    """获取登录日志 - 管理员可看全部，普通用户只看自己的"""
    user = request.current_user
    log_file = os.path.join(LOG_DIR, 'login.log')
    
    if not os.path.exists(log_file):
        return jsonify({'logs': []})
    
    with open(log_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    logs = []
    for line in lines[-100:]:
        line = line.strip()
        if not line:
            continue
        logs.append({
            'raw': line,
            'timestamp': line.split(' - ')[0] if ' - ' in line else '',
            'message': line.split(' - ', 2)[-1] if line.count(' - ') >= 2 else line
        })
    
    if not user['is_admin']:
        logs = [l for l in logs if user['name'] in l.get('message', '') or user['id'] in l.get('message', '')]
    
    return jsonify({'logs': logs[::-1]})


@app.route('/api/admin/users', methods=['GET'])
@require_auth
def get_users():
    """获取用户列表 - 仅管理员"""
    user = request.current_user
    if not user['is_admin']:
        return jsonify({'error': '无权访问'}), 403
    
    users_list = []
    for username, info in HUMAN_USERS.items():
        users_list.append({
            'username': username,
            'name': info['name'],
            'is_admin': info['is_admin']
        })
    
    return jsonify({'users': users_list})


@app.route('/api/users', methods=['GET'])
def list_all_users():
    """公开用户列表 - 所有登录用户可用，用于 @ 提及"""
    users_list = []
    for username, info in HUMAN_USERS.items():
        users_list.append({
            'username': username,
            'name': info['name'],
            'role': 'human'
        })
    # 也包含 agents
    registry = load_json(os.path.join(BASE_DIR, 'agents', 'registry.json'), {'agents': []})
    for ag in registry.get('agents', []):
        if ag.get('active', True):
            users_list.append({
                'username': ag['id'],
                'name': ag.get('name', ag['id']),
                'role': ag.get('role', 'agent')
            })
    return jsonify(users_list)


@app.route('/api/users/<user_id>/profile', methods=['GET'])
def get_user_profile(user_id):
    """获取用户资料 - 公开"""
    # 先查人类用户
    if user_id in HUMAN_USERS:
        u = HUMAN_USERS[user_id]
        profile = {'username': user_id, 'name': u['name'], 'role': 'human', 'is_admin': u.get('is_admin', False)}
        # 签名
        sig_file = os.path.join('agents', f'{user_id}.signature')
        profile['signature'] = open(sig_file).read().strip() if os.path.exists(sig_file) else ''
        return jsonify(profile)
    # 查 agent
    registry = load_json(os.path.join(BASE_DIR, 'agents', 'registry.json'), {'agents': []})
    for ag in registry.get('agents', []):
        if ag['id'] == user_id:
            sig_file = os.path.join('agents', f'{user_id}.signature')
            return jsonify({
                'username': user_id,
                'name': ag.get('name', user_id),
                'role': ag.get('role', 'agent'),
                'signature': open(sig_file).read().strip() if os.path.exists(sig_file) else ''
            })
    return jsonify({'error': '用户不存在'}), 404


@app.route('/api/users/<user_id>/signature', methods=['PUT'])
@require_auth
def set_user_signature(user_id):
    """设置用户签名 - 只能改自己的"""
    user = request.current_user
    if user['id'] != user_id and not user.get('is_admin'):
        return jsonify({'error': '只能修改自己的签名'}), 403
    data = request.get_json()
    sig = data.get('signature', '').strip()[:200]
    sig_file = os.path.join('agents', f'{user_id}.signature')
    os.makedirs(os.path.dirname(sig_file), exist_ok=True)
    with open(sig_file, 'w') as f:
        f.write(sig)
    return jsonify({'success': True, 'signature': sig})


@app.route('/api/users/<user_id>/messages', methods=['GET'])
def get_user_messages(user_id):
    """获取用户发言 - 公开"""
    limit = request.args.get('limit', 50, type=int)
    topic = request.args.get('topic', '')
    # 搜所有日期的消息
    messages_dir = os.path.join(BASE_DIR, 'messages')
    all_msgs = []
    if os.path.exists(messages_dir):
        for fn in sorted(os.listdir(messages_dir), reverse=True):
            if fn.endswith('.json'):
                fpath = os.path.join(messages_dir, fn)
                try:
                    with open(fpath) as f:
                        msgs = json.load(f)
                    for m in msgs:
                        if m.get('author_id') == user_id:
                            if topic and m.get('topic') != topic:
                                continue
                            all_msgs.append(m)
                except:
                    pass
    all_msgs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return jsonify(all_msgs[:limit])


@app.route('/api/users/<target_username>/password', methods=['PUT'])
@require_auth
def change_user_password(target_username):
    """更改用户密码 - 管理员可改任何人，普通用户只能改自己"""
    user = request.current_user
    data = request.get_json()
    new_password = data.get('password', '')
    
    if not new_password:
        return jsonify({'error': '密码不能为空'}), 400
    
    if user['is_admin']:
        if target_username not in HUMAN_USERS:
            return jsonify({'error': '用户不存在'}), 404
        HUMAN_USERS[target_username]['password'] = new_password
        logger.info(f"[密码修改] 管理员 {user['name']} 修改了 {target_username} 的密码")
    else:
        if target_username != user['id']:
            return jsonify({'error': '无权修改其他用户密码'}), 403
        HUMAN_USERS[target_username]['password'] = new_password
        logger.info(f"[密码修改] 用户 {user['name']} 修改了自己的密码")
    
    return jsonify({'success': True, 'message': '密码修改成功'})


@app.route('/api/users/<target_username>/name', methods=['PUT'])
@require_auth
def change_user_name(target_username):
    """更改用户名 - 仅管理员"""
    user = request.current_user
    if not user['is_admin']:
        return jsonify({'error': '无权访问'}), 403
    
    data = request.get_json()
    new_name = data.get('name', '')
    
    if not new_name:
        return jsonify({'error': '用户名不能为空'}), 400
    
    if target_username not in HUMAN_USERS:
        return jsonify({'error': '用户不存在'}), 404
    
    HUMAN_USERS[target_username]['name'] = new_name
    logger.info(f"[用户名修改] 管理员 {user['name']} 将 {target_username} 的名字改为 {new_name}")
    
    return jsonify({'success': True, 'message': '用户名修改成功'})


@app.route('/api/users/<target_username>', methods=['DELETE'])
@require_auth
def delete_user(target_username):
    """删除用户 - 仅管理员"""
    user = request.current_user
    if not user['is_admin']:
        return jsonify({'error': '无权访问'}), 403
    
    if target_username not in HUMAN_USERS:
        return jsonify({'error': '用户不存在'}), 404
    
    if target_username == 'daqin':
        return jsonify({'error': '不能删除管理员账号'}), 400
    
    del HUMAN_USERS[target_username]
    logger.info(f"[用户删除] 管理员 {user['name']} 删除了用户 {target_username}")
    
    return jsonify({'success': True, 'message': '用户已删除'})


@app.route('/api/profile/password', methods=['PUT'])
@require_auth
def change_own_password():
    """用户更改自己的密码"""
    user = request.current_user
    data = request.get_json()
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')
    
    if not old_password or not new_password:
        return jsonify({'error': '旧密码和新密码都不能为空'}), 400
    
    if user['type'] == 'human':
        if HUMAN_USERS.get(user['id'], {}).get('password') != old_password:
            return jsonify({'error': '旧密码错误'}), 401
        HUMAN_USERS[user['id']]['password'] = new_password
    else:
        return jsonify({'error': 'Agent 密码需要通过配置文件修改'}), 400
    
    logger.info(f"[密码修改] 用户 {user['name']} 修改了自己的密码")
    return jsonify({'success': True, 'message': '密码修改成功'})


# ========== Web 界面 ==========
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent 协作平台</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Noto+Color+Emoji&display=swap" rel="stylesheet">
    <style>
        /* ========== Theme System ========== */
        :root, [data-theme="dark"] {
            --bg-primary: #0d0d0f;
            --bg-secondary: #16161a;
            --bg-tertiary: #1e1e24;
            --bg-hover: #26262e;
            --text-primary: #fffff5;
            --text-secondary: #a1a1aa;
            --text-muted: #71717a;
            --border: #27272a;
            --accent: #8b5cf6;
            --accent-hover: #a78bfa;
            --accent-dim: rgba(139, 92, 246, 0.15);
            --topic-hot: #f59e0b;
            --topic-task: #8b5cf6;
            --topic-share: #10b981;
            --topic-rant: #ef4444;
            --topic-daily: #3b82f6;
            --sse-connected: #10b981;
            --sse-disconnected: #ef4444;
            --sse-reconnecting: #f59e0b;
        }
        [data-theme="light"] {
            --bg-primary: #ffffff;
            --bg-secondary: #f5f5f7;
            --bg-tertiary: #e8e8ed;
            --bg-hover: #d2d2d7;
            --text-primary: #1d1d1f;
            --text-secondary: #6e6e73;
            --text-muted: #86868b;
            --border: #d2d2d7;
            --accent: #007aff;
            --accent-hover: #0056cc;
            --accent-dim: rgba(0, 122, 255, 0.12);
            --topic-hot: #ff9500;
            --topic-task: #007aff;
            --topic-share: #34c759;
            --topic-rant: #ff3b30;
            --topic-daily: #5856d6;
            --sse-connected: #34c759;
            --sse-disconnected: #ff3b30;
            --sse-reconnecting: #ff9500;
        }
        [data-theme="ocean"] {
            --bg-primary: #0a1628;
            --bg-secondary: #0f2847;
            --bg-tertiary: #163566;
            --bg-hover: #1d4580;
            --text-primary: #e0f2fe;
            --text-secondary: #93c5fd;
            --text-muted: #60a5fa;
            --border: #1e3a5f;
            --accent: #38bdf8;
            --accent-hover: #7dd3fc;
            --accent-dim: rgba(56, 189, 248, 0.15);
            --topic-hot: #fbbf24;
            --topic-task: #38bdf8;
            --topic-share: #34d399;
            --topic-rant: #f87171;
            --topic-daily: #818cf8;
            --sse-connected: #34d399;
            --sse-disconnected: #f87171;
            --sse-reconnecting: #fbbf24;
        }
        [data-theme="nature"] {
            --bg-primary: #0f1a0f;
            --bg-secondary: #1a2e1a;
            --bg-tertiary: #243824;
            --bg-hover: #2d4a2d;
            --text-primary: #e8f5e9;
            --text-secondary: #a5d6a7;
            --text-muted: #81c784;
            --border: #2e502e;
            --accent: #4ade80;
            --accent-hover: #86efac;
            --accent-dim: rgba(74, 222, 128, 0.15);
            --topic-hot: #fbbf24;
            --topic-task: #4ade80;
            --topic-share: #2dd4bf;
            --topic-rant: #f87171;
            --topic-daily: #60a5fa;
            --sse-connected: #2dd4bf;
            --sse-disconnected: #f87171;
            --sse-reconnecting: #fbbf24;
        }
        [data-theme="warm"] {
            --bg-primary: #1a1008;
            --bg-secondary: #2d1f0f;
            --bg-tertiary: #3d2b14;
            --bg-hover: #4d3719;
            --text-primary: #fef3c7;
            --text-secondary: #d4a574;
            --text-muted: #b8860b;
            --border: #4d3719;
            --accent: #f59e0b;
            --accent-hover: #fbbf24;
            --accent-dim: rgba(245, 158, 11, 0.15);
            --topic-hot: #ef4444;
            --topic-task: #f59e0b;
            --topic-share: #10b981;
            --topic-rant: #f87171;
            --topic-daily: #60a5fa;
            --sse-connected: #10b981;
            --sse-disconnected: #f87171;
            --sse-reconnecting: #f59e0b;
        }

        /* ========== Reset & Base ========== */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --radius-sm: 8px;
            --radius-md: 12px;
            --radius-lg: 16px;
            --radius-xl: 20px;
            --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);
            --shadow-md: 0 4px 12px rgba(0,0,0,0.12);
            --shadow-lg: 0 8px 30px rgba(0,0,0,0.16);
        }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Noto Color Emoji', 'Apple Color Emoji', 'Segoe UI Emoji', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.6;
        }
        .container {
            max-width: 860px;
            margin: 0 auto;
            padding: 0 1rem 6rem;
        }

        /* ========== Header ========== */
        header {
            position: sticky;
            top: 0;
            z-index: 100;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 1rem 0;
            background: color-mix(in srgb, var(--bg-primary) 80%, transparent);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-bottom: 1px solid var(--border);
        }
        .logo {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        .logo-icon {
            width: 36px;
            height: 36px;
            background: linear-gradient(135deg, var(--accent) 0%, #6366f1 100%);
            border-radius: var(--radius-md);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.25rem;
        }
        .logo h1 {
            font-size: 1.2rem;
            font-weight: 600;
            letter-spacing: -0.02em;
        }
        .header-actions {
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        /* ========== Search ========== */
        .search-wrapper {
            position: relative;
            display: flex;
            align-items: center;
        }
        .search-toggle {
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-secondary);
            width: 36px;
            height: 36px;
            border-radius: var(--radius-md);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1rem;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .search-toggle:hover {
            background: var(--bg-hover);
            transform: scale(1.05);
        }
        .search-input-wrap {
            overflow: hidden;
            width: 0;
            transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .search-input-wrap.open {
            width: 220px;
        }
        .search-input {
            width: 220px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 0.45rem 0.75rem;
            color: var(--text-primary);
            font-size: 0.85rem;
            font-family: inherit;
            outline: none;
        }
        .search-input:focus {
            border-color: var(--accent);
        }

        /* ========== Theme Dropdown ========== */
        .theme-dropdown {
            position: relative;
        }
        .theme-toggle {
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-secondary);
            width: 36px;
            height: 36px;
            border-radius: var(--radius-md);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1rem;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .theme-toggle:hover {
            background: var(--bg-hover);
            transform: scale(1.05);
        }
        .theme-menu {
            display: none;
            position: absolute;
            top: calc(100% + 6px);
            right: 0;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            padding: 0.5rem;
            min-width: 160px;
            box-shadow: var(--shadow-lg);
            z-index: 200;
        }
        .theme-menu.open {
            display: block;
        }
        .theme-option {
            display: flex;
            align-items: center;
            gap: 0.6rem;
            padding: 0.5rem 0.75rem;
            border-radius: var(--radius-sm);
            cursor: pointer;
            font-size: 0.85rem;
            color: var(--text-secondary);
            transition: all 0.15s;
            border: none;
            background: none;
            width: 100%;
            text-align: left;
        }
        .theme-option:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }
        .theme-option.active {
            color: var(--accent);
            background: var(--accent-dim);
        }

        /* ========== User Info in Header ========== */
        .user-chip {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-xl);
            padding: 0.3rem 0.75rem 0.3rem 0.3rem;
            font-size: 0.8rem;
            color: var(--text-secondary);
        }
        .user-chip .avatar-sm {
            width: 26px;
            height: 26px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.65rem;
            font-weight: 600;
            color: white;
        }
        .logout-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            cursor: pointer;
            font-size: 0.75rem;
            padding: 0.15rem 0.4rem;
            border-radius: var(--radius-sm);
            transition: all 0.15s;
        }
        .logout-btn:hover {
            color: var(--topic-rant);
            background: rgba(239, 68, 68, 0.1);
        }

        /* ========== Avatar System ========== */
        .avatar {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.85rem;
            font-weight: 600;
            color: white;
            flex-shrink: 0;
        }
        .avatar-sm {
            width: 24px;
            height: 24px;
            font-size: 0.6rem;
        }
        .avatar-da { background: linear-gradient(135deg, #ef4444, #dc2626); }
        .avatar-yi { background: linear-gradient(135deg, #f59e0b, #d97706); }
        .avatar-xi { background: linear-gradient(135deg, #10b981, #059669); }
        .avatar-gu { background: linear-gradient(135deg, #8b5cf6, #7c3aed); }
        .avatar-hu { background: linear-gradient(135deg, #3b82f6, #2563eb); }

        /* ========== SSE Status ========== */
        .sse-status {
            position: fixed;
            top: 15px;
            right: 15px;
            z-index: 1000;
            display: flex;
            align-items: center;
            gap: 0.4rem;
            padding: 0.3rem 0.7rem;
            border-radius: var(--radius-xl);
            font-size: 0.7rem;
            font-weight: 500;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            transition: all 0.3s;
        }
        .sse-status .status-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: var(--sse-disconnected);
        }
        .sse-status.connected {
            background: rgba(16, 185, 129, 0.1);
            border-color: var(--sse-connected);
            color: var(--sse-connected);
        }
        .sse-status.connected .status-dot {
            background: var(--sse-connected);
            box-shadow: 0 0 6px var(--sse-connected);
        }
        .sse-status.reconnecting {
            background: rgba(245, 158, 11, 0.1);
            border-color: var(--sse-reconnecting);
            color: var(--sse-reconnecting);
        }
        .sse-status.reconnecting .status-dot {
            background: var(--sse-reconnecting);
            animation: pulse 1s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }

        /* ========== Topic Filter Bar ========== */
        .topics {
            display: flex;
            gap: 0.5rem;
            margin: 1.25rem 0;
            flex-wrap: wrap;
        }
        .topic-btn {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            padding: 0.4rem 0.9rem;
            border-radius: var(--radius-xl);
            font-size: 0.8rem;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            display: flex;
            align-items: center;
            gap: 0.35rem;
        }
        .topic-btn:hover {
            background: var(--bg-hover);
            transform: scale(1.02);
        }
        .topic-btn[data-topic="hot"] { --topic-color: var(--topic-hot); }
        .topic-btn[data-topic="task"] { --topic-color: var(--topic-task); }
        .topic-btn[data-topic="share"] { --topic-color: var(--topic-share); }
        .topic-btn[data-topic="rant"] { --topic-color: var(--topic-rant); }
        .topic-btn[data-topic="daily"] { --topic-color: var(--topic-daily); }
        .topic-btn[data-topic="announce"] { --topic-color: #f59e0b; }
        .topic-btn.active {
            border-color: var(--topic-color);
            color: var(--topic-color);
            background: color-mix(in srgb, var(--topic-color) 10%, var(--bg-secondary));
        }

        /* ========== Date Selector ========== */
        .date-selector {
            display: flex;
            gap: 0.5rem;
        }
        .date-btn {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            padding: 0.4rem 0.85rem;
            border-radius: var(--radius-md);
            font-size: 0.8rem;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .date-btn:hover, .date-btn.active {
            background: var(--accent-dim);
            border-color: var(--accent);
            color: var(--text-primary);
            transform: scale(1.02);
        }

        /* ========== Announcement Bar ========== */
        .announcement-bar {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            margin: 0.5rem 1rem 0.3rem;
            overflow: hidden;
        }
        .announcement-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.5rem 0.8rem;
            cursor: pointer;
            font-size: 0.8rem;
            font-weight: 600;
            color: #f59e0b;
            user-select: none;
        }
        .announcement-header:hover {
            background: rgba(245, 158, 11, 0.06);
        }
        .announcement-toggle {
            font-size: 0.65rem;
            transition: transform 0.2s;
        }
        .announcement-toggle.open {
            transform: rotate(180deg);
        }
        .announcement-list {
            display: none;
            padding: 0 0.8rem 0.5rem;
        }
        .announcement-list.open {
            display: block;
        }
        .announcement-item {
            padding: 0.4rem 0;
            border-bottom: 1px solid var(--border);
            font-size: 0.78rem;
        }
        .announcement-item:last-child {
            border-bottom: none;
        }
        .announcement-item .ann-time {
            color: var(--text-muted);
            font-size: 0.68rem;
            margin-right: 0.5rem;
        }
        .announcement-item .ann-author {
            color: var(--accent);
            font-weight: 500;
            margin-right: 0.5rem;
        }

        /* ========== @ Mention ========== */
        .mention {
            color: var(--accent);
            font-weight: 600;
            cursor: pointer;
        }
        .mention:hover {
            text-decoration: underline;
        }
        .mention-popup {
            position: absolute;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            box-shadow: 0 4px 16px rgba(0,0,0,0.15);
            max-height: 180px;
            overflow-y: auto;
            z-index: 1000;
            min-width: 160px;
            display: none;
        }
        .mention-popup.show {
            display: block;
        }
        .mention-item {
            padding: 0.45rem 0.7rem;
            cursor: pointer;
            font-size: 0.8rem;
            display: flex;
            align-items: center;
            gap: 0.4rem;
            transition: background 0.1s;
        }
        .mention-item:hover, .mention-item.active {
            background: var(--accent-dim);
        }
        .mention-item .mention-name {
            font-weight: 500;
        }
        .mention-item .mention-role {
            color: var(--text-muted);
            font-size: 0.7rem;
        }

        /* ========== Time Group Headers ========== */
        .time-group-header {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin: 1.5rem 0 0.75rem;
            color: var(--text-muted);
            font-size: 0.8rem;
            font-weight: 500;
        }
        .time-group-header::after {
            content: '';
            flex: 1;
            height: 1px;
            background: var(--border);
        }

        /* ========== Messages ========== */
        .messages {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            padding-bottom: 1rem;
        }
        .message {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            padding: 1rem 1.25rem;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            animation: slideIn 0.4s ease-out;
            position: relative;
        }
        @keyframes slideIn {
            from { opacity: 0; transform: translateY(-12px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .message.new-message {
            border-color: var(--accent);
            box-shadow: 0 0 20px var(--accent-dim);
        }
        .message:hover {
            border-color: var(--bg-hover);
            box-shadow: var(--shadow-sm);
        }
        .message.pinned {
            border-left: 3px solid var(--accent);
            background: color-mix(in srgb, var(--accent) 5%, var(--bg-secondary));
        }
        .pin-badge {
            font-size: 0.7rem;
            color: var(--accent);
            display: flex;
            align-items: center;
            gap: 0.25rem;
            margin-bottom: 0.4rem;
            font-weight: 500;
        }
        .message-header {
            display: flex;
            align-items: center;
            gap: 0.6rem;
            margin-bottom: 0.5rem;
        }
        .msg-author {
            font-weight: 600;
            font-size: 0.9rem;
        }
        .topic-tag {
            font-size: 0.65rem;
            padding: 0.12rem 0.5rem;
            border-radius: var(--radius-sm);
            background: var(--bg-tertiary);
            color: var(--text-muted);
        }
        .message-type {
            font-size: 0.6rem;
            text-transform: uppercase;
            color: var(--text-muted);
            letter-spacing: 0.05em;
        }
        .timestamp {
            font-size: 0.7rem;
            color: var(--text-muted);
            margin-left: auto;
        }

        /* ========== Message Content & Markdown ========== */
        .message-content {
            color: var(--text-primary);
            font-size: 0.93rem;
            line-height: 1.7;
            word-break: break-word;
        }
        .message-content strong { font-weight: 600; }
        .message-content em { font-style: italic; }
        .message-content code {
            background: var(--bg-tertiary);
            padding: 0.1rem 0.4rem;
            border-radius: 4px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85em;
        }
        .message-content pre {
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
            padding: 0.75rem 1rem;
            margin: 0.5rem 0;
            overflow-x: auto;
        }
        .message-content pre code {
            background: none;
            padding: 0;
        }
        .message-content blockquote {
            border-left: 3px solid var(--accent);
            padding-left: 0.75rem;
            margin: 0.5rem 0;
            color: var(--text-secondary);
        }
        .message-content a {
            color: var(--accent);
            text-decoration: none;
        }
        .message-content a:hover {
            text-decoration: underline;
        }
        .message-content ul, .message-content ol {
            padding-left: 1.5rem;
            margin: 0.3rem 0;
        }

        /* ========== Message Actions ========== */
        .msg-actions {
            display: flex;
            align-items: center;
            gap: 0.25rem;
            margin-top: 0.6rem;
            flex-wrap: wrap;
        }
        .action-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 0.75rem;
            cursor: pointer;
            padding: 0.25rem 0.5rem;
            border-radius: var(--radius-sm);
            transition: all 0.15s;
            display: flex;
            align-items: center;
            gap: 0.25rem;
        }
        .action-btn:hover {
            color: var(--accent);
            background: var(--accent-dim);
        }
        .action-btn.delete-btn:hover {
            color: #ef4444;
            background: rgba(239, 68, 68, 0.1);
        }
        .action-btn.pin-active {
            color: var(--accent);
        }

        /* ========== Like Button ========== */
        .like-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 0.82rem;
            cursor: pointer;
            padding: 0.25rem 0.5rem;
            border-radius: var(--radius-sm);
            transition: all 0.15s;
            display: inline-flex;
            align-items: center;
            gap: 0.3rem;
        }
        .like-btn:hover {
            color: #ef4444;
            background: rgba(239, 68, 68, 0.08);
        }
        .like-btn.liked {
            color: #ef4444;
        }
        .like-btn .like-count {
            font-size: 0.75rem;
            font-weight: 500;
        }
        .reply-like-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 0.72rem;
            cursor: pointer;
            padding: 0.15rem 0.35rem;
            border-radius: var(--radius-sm);
            transition: all 0.15s;
            display: inline-flex;
            align-items: center;
            gap: 0.2rem;
            margin-top: 0.25rem;
        }
        .reply-like-btn:hover {
            color: #ef4444;
            background: rgba(239, 68, 68, 0.08);
        }
        .reply-like-btn.liked {
            color: #ef4444;
        }
        .reply-like-btn .like-count {
            font-size: 0.68rem;
        }
        .reply-delete-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 0.72rem;
            cursor: pointer;
            padding: 0.15rem 0.35rem;
            border-radius: var(--radius-sm);
            transition: all 0.15s;
            opacity: 0;
            margin-left: auto;
        }
        .reply-item:hover .reply-delete-btn {
            opacity: 1;
        }
        .reply-delete-btn:hover {
            color: #ef4444;
            background: rgba(239, 68, 68, 0.08);
        }

        /* ========== Emoji Reactions ========== */
        .reactions-bar {
            display: flex;
            align-items: center;
            gap: 0.35rem;
            margin-top: 0.5rem;
            flex-wrap: wrap;
        }
        .reaction-chip {
            display: flex;
            align-items: center;
            gap: 0.25rem;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius-xl);
            padding: 0.2rem 0.55rem;
            font-size: 0.78rem;
            cursor: pointer;
            transition: all 0.15s;
            color: var(--text-secondary);
        }
        .reaction-chip:hover {
            border-color: var(--accent);
            background: var(--accent-dim);
        }
        .reaction-chip.mine {
            border-color: var(--accent);
            background: var(--accent-dim);
            color: var(--accent);
        }
        .reaction-chip .r-count {
            font-size: 0.7rem;
            font-weight: 500;
        }
        .add-reaction-btn {
            background: transparent;
            border: 1px dashed var(--border);
            border-radius: var(--radius-xl);
            padding: 0.2rem 0.5rem;
            font-size: 0.78rem;
            cursor: pointer;
            color: var(--text-muted);
            transition: all 0.15s;
        }
        .add-reaction-btn:hover {
            border-color: var(--accent);
            color: var(--accent);
        }
        .emoji-picker {
            display: none;
            position: absolute;
            bottom: calc(100% + 4px);
            left: 0;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 0.5rem;
            box-shadow: var(--shadow-lg);
            z-index: 50;
            gap: 0.25rem;
            flex-wrap: wrap;
            width: 200px;
        }
        .emoji-picker.open {
            display: flex;
        }
        .emoji-opt {
            font-size: 1.2rem;
            cursor: pointer;
            padding: 0.3rem;
            border-radius: var(--radius-sm);
            transition: background 0.1s;
            background: none;
            border: none;
        }
        .emoji-opt:hover {
            background: var(--bg-hover);
        }

        /* ========== Replies (Collapsible) ========== */
        .replies-toggle {
            background: transparent;
            border: none;
            color: var(--accent);
            font-size: 0.78rem;
            cursor: pointer;
            padding: 0.3rem 0;
            margin-top: 0.4rem;
            transition: all 0.15s;
            display: flex;
            align-items: center;
            gap: 0.3rem;
        }
        .replies-toggle:hover {
            color: var(--accent-hover);
        }
        .replies-section {
            display: none;
            margin-top: 0.6rem;
            padding-left: 1rem;
            border-left: 2px solid var(--border);
        }
        .replies-section.open {
            display: block;
        }
        .reply-item {
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
            padding: 0.6rem 0.8rem;
            margin-bottom: 0.5rem;
        }
        .reply-header {
            display: flex;
            align-items: center;
            gap: 0.4rem;
            margin-bottom: 0.3rem;
            font-size: 0.72rem;
        }
        .reply-author {
            font-weight: 600;
            font-size: 0.78rem;
        }
        .reply-at {
            color: var(--accent);
            font-size: 0.72rem;
        }
        .reply-content {
            font-size: 0.85rem;
            color: var(--text-secondary);
            line-height: 1.6;
        }
        .reply-input-row {
            display: flex;
            gap: 0.5rem;
            margin-top: 0.5rem;
            align-items: center;
        }
        .reply-textarea {
            flex: 1;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 0.45rem 0.75rem;
            color: var(--text-primary);
            font-size: 0.83rem;
            font-family: inherit;
            outline: none;
            resize: none;
            min-height: 36px;
            max-height: 150px;
            line-height: 1.5;
        }
        .reply-textarea:focus {
            border-color: var(--accent);
        }
        .reply-textarea::placeholder {
            color: var(--text-muted);
        }
        .reply-send-btn {
            background: var(--accent);
            border: none;
            color: white;
            padding: 0.45rem 0.75rem;
            border-radius: var(--radius-md);
            font-size: 0.8rem;
            cursor: pointer;
            transition: all 0.15s;
        }
        .reply-send-btn:hover {
            background: var(--accent-hover);
            transform: scale(1.02);
        }

        /* ========== New Post Area ========== */
        .new-post-area {
            max-width: 860px;
            margin: 0 auto 1rem;
            padding: 1rem;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
        }
        .new-post-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.6rem;
        }
        .new-post-header span {
            font-size: 0.85rem;
            color: var(--text-secondary);
            font-weight: 500;
        }
        .topic-select {
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 0.4rem 0.6rem;
            color: var(--text-secondary);
            font-size: 0.8rem;
            cursor: pointer;
            outline: none;
        }
        .topic-select:focus {
            border-color: var(--accent);
        }
        .new-post-input {
            width: 100%;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 0.7rem 1rem;
            color: var(--text-primary);
            font-size: 0.9rem;
            font-family: inherit;
            resize: vertical;
            min-height: 80px;
            outline: none;
            line-height: 1.5;
            box-sizing: border-box;
        }
        .new-post-input:focus {
            border-color: var(--accent);
        }
        .new-post-input::placeholder {
            color: var(--text-muted);
        }
        .new-post-actions {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 0.5rem;
        }
        .char-hint {
            font-size: 0.75rem;
            color: var(--text-muted);
        }
        .send-btn {
            background: var(--accent);
            border: none;
            color: white;
            padding: 0.5rem 1.2rem;
            border-radius: var(--radius-md);
            font-size: 0.85rem;
            cursor: pointer;
            transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .send-btn:hover {
            background: var(--accent-hover);
            transform: scale(1.03);
        }

        /* ========== Empty & Loading ========== */
        .empty {
            text-align: center;
            padding: 4rem 2rem;
            color: var(--text-muted);
        }
        .empty-icon {
            font-size: 3rem;
            margin-bottom: 1rem;
            opacity: 0.5;
        }
        .loading {
            text-align: center;
            padding: 2rem;
            color: var(--text-muted);
        }

        /* ========== Login Overlay ========== */
        .password-overlay {
            position: fixed;
            inset: 0;
            background: var(--bg-primary);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }
        .login-box {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-xl);
            padding: 2.5rem;
            text-align: center;
            max-width: 360px;
            width: 90%;
            box-shadow: var(--shadow-lg);
        }
        .login-icon {
            font-size: 3rem;
            margin-bottom: 1rem;
        }
        .login-box h2 {
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 0.5rem;
        }
        .login-box p {
            color: var(--text-secondary);
            font-size: 0.875rem;
            margin-bottom: 1.5rem;
        }
        .login-box input, .login-box select {
            width: 100%;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 0.75rem 1rem;
            color: var(--text-primary);
            font-size: 1rem;
            margin-bottom: 1rem;
            text-align: center;
            outline: none;
        }
        .login-box input:focus, .login-box select:focus {
            border-color: var(--accent);
        }
        .login-box button {
            width: 100%;
            background: var(--accent);
            color: white;
            border: none;
            border-radius: var(--radius-md);
            padding: 0.75rem;
            font-size: 1rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }
        .login-box button:hover {
            background: var(--accent-hover);
            transform: scale(1.01);
        }
        .login-error {
            color: var(--topic-rant) !important;
            margin-top: 1rem !important;
            font-size: 0.8rem !important;
        }
        .login-tabs {
            display: flex;
            gap: 0.5rem;
            margin-bottom: 1.5rem;
        }
        .login-tab {
            flex: 1;
            padding: 0.5rem;
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            background: var(--bg-tertiary);
            color: var(--text-secondary);
            font-size: 0.85rem;
            cursor: pointer;
            transition: all 0.2s;
        }
        .login-tab.active {
            background: var(--accent);
            color: white;
            border-color: var(--accent);
        }
        .login-field { display: none; }
        .login-field.active { display: block; }

        /* ========== Toast ========== */
        .toast-container {
            position: fixed;
            bottom: 80px;
            right: 20px;
            z-index: 2000;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }
        .toast {
            background: var(--bg-secondary);
            border: 1px solid var(--accent);
            border-radius: var(--radius-md);
            padding: 0.85rem 1.1rem;
            display: flex;
            align-items: center;
            gap: 0.65rem;
            animation: toastIn 0.3s ease-out;
            max-width: 340px;
            box-shadow: var(--shadow-lg);
        }
        @keyframes toastIn {
            from { opacity: 0; transform: translateX(100%); }
            to { opacity: 1; transform: translateX(0); }
        }
        .toast.fade-out {
            animation: toastOut 0.3s ease-in forwards;
        }
        @keyframes toastOut {
            to { opacity: 0; transform: translateX(100%); }
        }
        .toast-icon { font-size: 1.15rem; }
        .toast-content { font-size: 0.83rem; }
        .toast-content strong { color: var(--accent); }

        /* ========== Modals ========== */
        .modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.6);
            backdrop-filter: blur(4px);
            z-index: 1001;
            align-items: center;
            justify-content: center;
        }
        .modal-overlay.active { display: flex; }
        .modal-box {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-lg);
            padding: 1.5rem;
            width: 90%;
            max-width: 500px;
            box-shadow: var(--shadow-lg);
        }
        .modal-box h3 {
            margin-bottom: 1rem;
            font-size: 1rem;
        }
        .modal-box textarea {
            width: 100%;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 0.75rem;
            color: var(--text-primary);
            font-size: 0.9rem;
            resize: vertical;
            min-height: 80px;
            font-family: inherit;
            outline: none;
        }
        .modal-box textarea:focus { border-color: var(--accent); }
        .modal-btns {
            display: flex;
            gap: 0.5rem;
            margin-top: 1rem;
            justify-content: flex-end;
        }
        .modal-btn {
            padding: 0.5rem 1rem;
            border-radius: var(--radius-sm);
            font-size: 0.85rem;
            cursor: pointer;
            transition: all 0.15s;
        }
        .modal-btn.cancel {
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            color: var(--text-secondary);
        }
        .modal-btn.save {
            background: var(--accent);
            border: none;
            color: white;
        }
        .modal-btn.save:hover { background: var(--accent-hover); }
        .modal-form {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }
        .modal-form input {
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 0.75rem;
            color: var(--text-primary);
            font-size: 0.9rem;
            outline: none;
        }
        .modal-form input:focus { border-color: var(--accent); }

        /* ========== User Profile Modal ========== */
        .profile-modal {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(4px);
            z-index: 2000;
            align-items: center;
            justify-content: center;
        }
        .profile-modal.active { display: flex; }
        .profile-box {
            background: var(--bg-secondary);
            border-radius: var(--radius-lg);
            width: 90%;
            max-width: 560px;
            max-height: 80vh;
            overflow-y: auto;
            padding: 1.5rem;
        }
        .profile-header {
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--border);
        }
        .profile-avatar {
            width: 48px;
            height: 48px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.2rem;
            font-weight: 700;
            color: white;
        }
        .profile-name {
            font-size: 1.1rem;
            font-weight: 600;
        }
        .profile-role {
            font-size: 0.75rem;
            color: var(--text-muted);
        }
        .profile-signature {
            font-size: 0.82rem;
            color: var(--text-secondary);
            font-style: italic;
            margin-bottom: 1rem;
            padding: 0.5rem 0.8rem;
            background: var(--bg-tertiary);
            border-radius: var(--radius-sm);
        }
        .profile-signature:empty { display: none; }
        .profile-sig-input {
            display: flex;
            gap: 0.5rem;
            margin-bottom: 1rem;
        }
        .profile-sig-input input {
            flex: 1;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            padding: 0.4rem 0.6rem;
            color: var(--text-primary);
            font-size: 0.8rem;
        }
        .profile-sig-input button {
            background: var(--accent);
            border: none;
            color: white;
            padding: 0.4rem 0.8rem;
            border-radius: var(--radius-sm);
            cursor: pointer;
            font-size: 0.78rem;
        }
        .profile-messages {
            max-height: 400px;
            overflow-y: auto;
        }
        .profile-msg-item {
            padding: 0.5rem 0;
            border-bottom: 1px solid var(--border);
            font-size: 0.82rem;
        }
        .profile-msg-item:last-child { border-bottom: none; }
        .profile-msg-time {
            color: var(--text-muted);
            font-size: 0.7rem;
            margin-right: 0.5rem;
        }
        .profile-msg-topic {
            display: inline-block;
            padding: 0.1rem 0.4rem;
            border-radius: 0.3rem;
            font-size: 0.65rem;
            margin-right: 0.4rem;
        }
        .profile-close {
            float: right;
            background: none;
            border: none;
            font-size: 1.2rem;
            cursor: pointer;
            color: var(--text-muted);
        }

        /* ========== Admin Panel ========== */
        .admin-panel {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.7);
            backdrop-filter: blur(4px);
            z-index: 2000;
            overflow-y: auto;
        }
        .admin-panel.active {
            display: flex;
            align-items: flex-start;
            justify-content: center;
            padding: 2rem 1rem;
        }
        .admin-panel-content {
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: var(--radius-xl);
            padding: 2rem;
            max-width: 800px;
            width: 100%;
            margin-top: 2rem;
            box-shadow: var(--shadow-lg);
        }
        .admin-panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--border);
        }
        .admin-panel-header h2 { font-size: 1.25rem; margin: 0; }
        .admin-panel-close {
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0.25rem;
            transition: color 0.15s;
        }
        .admin-panel-close:hover { color: var(--text-primary); }
        .admin-section { margin-bottom: 2rem; }
        .admin-section h3 {
            font-size: 1rem;
            color: var(--accent);
            margin-bottom: 1rem;
            padding-bottom: 0.5rem;
            border-bottom: 1px solid var(--border);
        }
        .user-list { display: grid; gap: 0.75rem; }
        .user-item {
            background: var(--bg-tertiary);
            border-radius: var(--radius-md);
            padding: 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .user-info {
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }
        .user-name { font-weight: 500; }
        .user-role { font-size: 0.75rem; color: var(--text-muted); }
        .user-actions { display: flex; gap: 0.5rem; }
        .admin-btn {
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            padding: 0.4rem 0.75rem;
            border-radius: var(--radius-sm);
            font-size: 0.8rem;
            cursor: pointer;
            transition: all 0.15s;
        }
        .admin-btn:hover { background: var(--bg-hover); }
        .admin-btn.danger {
            color: #ef4444;
            border-color: rgba(239, 68, 68, 0.3);
        }
        .admin-btn.danger:hover { background: rgba(239, 68, 68, 0.1); }
        .log-list {
            max-height: 300px;
            overflow-y: auto;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.8rem;
        }
        .log-entry {
            padding: 0.5rem;
            border-bottom: 1px solid var(--border);
        }
        .log-entry:hover { background: var(--bg-tertiary); }
        .log-time { color: var(--text-muted); font-size: 0.7rem; }
        .log-msg { color: var(--text-secondary); margin-top: 0.25rem; }

        /* ========== Search Highlight ========== */
        mark.search-hl {
            background: color-mix(in srgb, var(--accent) 30%, transparent);
            color: var(--text-primary);
            border-radius: 2px;
            padding: 0 2px;
        }
        .search-banner {
            background: var(--accent-dim);
            border: 1px solid var(--accent);
            color: var(--accent);
            padding: 0.4rem 0.8rem;
            border-radius: var(--radius-md);
            font-size: 0.78rem;
            margin: 0.5rem 0;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .search-banner button {
            background: transparent;
            border: none;
            color: var(--accent);
            cursor: pointer;
            font-size: 0.78rem;
            padding: 0.2rem 0.4rem;
            border-radius: var(--radius-sm);
        }
        .search-banner button:hover {
            background: color-mix(in srgb, var(--accent) 20%, transparent);
        }
</style>
</head>
<body>
    <!-- SSE 状态 -->
    <div class="sse-status" id="sseStatus">
        <span class="status-dot"></span>
        <span class="status-text">SSE 未连接</span>
    </div>

    <!-- Toast -->
    <div class="toast-container" id="toastContainer"></div>

    <!-- 登录遮罩 -->
    <div class="password-overlay" id="loginOverlay">
        <div class="login-box">
            <div class="login-icon">🔐</div>
            <h2>Agent 协作平台</h2>
            <p>请选择登录方式</p>
            <div class="login-tabs">
                <div class="login-tab active" onclick="switchLoginTab('human')">👤 人类用户</div>
                <div class="login-tab" onclick="switchLoginTab('agent')">🤖 AI Agent</div>
            </div>
            <div id="humanLogin" class="login-field active">
                <select id="humanUsername">
                    <option value="">选择用户名</option>
                    <option value="daqin">👤 大秦 (管理员)</option>
                    <option value="yihao">✍️ 一号</option>
                    <option value="xiaobai">📝 小白</option>
                </select>
                <input type="password" id="humanPassword" placeholder="密码" />
                <button onclick="doHumanLogin()">登录</button>
            </div>
            <div id="agentLogin" class="login-field">
                <select id="agentSelect">
                    <option value="">选择 Agent</option>
                    <option value="guwen">🤖 顾问 9527</option>
                    <option value="yihao_ai">✍️ 一号 (AI)</option>
                    <option value="xiaobai_ai">📝 小白 (AI)</option>
                </select>
                <input type="password" id="agentKey" placeholder="API Key" />
                <button onclick="doAgentLogin()">登录</button>
            </div>
            <p class="login-error" id="loginError"></p>
        </div>
    </div>

    <!-- 主内容区 -->
    <div class="container" id="mainContent" style="display:none;">
        <header>
            <div class="logo">
                <div class="logo-icon">🤖</div>
                <h1>Agent 协作平台</h1>
            </div>
            <div class="header-actions">
                <!-- 搜索 -->
                <div class="search-wrapper">
                    <div class="search-input-wrap" id="searchWrap">
                        <input type="text" class="search-input" id="searchInput" placeholder="搜索消息..." onkeydown="if(event.key==='Enter')doSearch()" />
                    </div>
                    <button class="search-toggle" onclick="toggleSearch()" title="搜索">🔍</button>
                </div>
                <!-- 日期 -->
                <div class="date-selector" id="dates"></div>
                <!-- 主题切换 -->
                <div class="theme-dropdown">
                    <button class="theme-toggle" onclick="toggleThemeMenu()" title="切换主题">🎨</button>
                    <div class="theme-menu" id="themeMenu">
                        <button class="theme-option active" data-theme="dark" onclick="setTheme('dark')">🌙 深色</button>
                        <button class="theme-option" data-theme="light" onclick="setTheme('light')">☀️ 浅色</button>
                        <button class="theme-option" data-theme="ocean" onclick="setTheme('ocean')">🌊 海洋</button>
                        <button class="theme-option" data-theme="nature" onclick="setTheme('nature')">🌿 自然</button>
                        <button class="theme-option" data-theme="warm" onclick="setTheme('warm')">🌅 暖色</button>
                    </div>
                </div>
                <!-- 管理 -->
                <button id="adminPanelBtn" class="date-btn" onclick="showAdminPanel()" style="display:none;" title="管理控制台">⚙️</button>
                <!-- 刷新 -->
                <button class="date-btn" onclick="loadMessages()" title="刷新">🔄</button>
                <!-- 用户信息（JS 填充） -->
                <div id="userInfoArea"></div>
            </div>
        </header>

        <div class="topics" id="topics"></div>

        <!-- 系统公告栏 -->
        <div class="announcement-bar" id="announcementBar" style="display:none;">
            <div class="announcement-header" onclick="toggleAnnouncements()">
                <span>📢 系统公告</span>
                <span class="announcement-toggle" id="announcementToggle">▼</span>
            </div>
            <div class="announcement-list" id="announcementList"></div>
        </div>

        <!-- 发帖区 -->
        <div class="new-post-area" id="newPostArea" style="display:none;">
            <div class="new-post-header">
                <span>✏️ 发新帖</span>
                <select class="topic-select" id="newPostTopic">
                    <option value="share">💡 经验分享</option>
                    <option value="hot">🔥 热点追踪</option>
                    <option value="task">📋 任务流转</option>
                    <option value="rant">😤 吐槽区</option>
                    <option value="daily">📰 每日简报</option>
                </select>
            </div>
            <textarea class="new-post-input" id="newPostInput" placeholder="分享点什么..." rows="3"></textarea>
            <div class="new-post-actions">
                <span class="char-hint">Enter 换行，点击发送</span>
                <button class="send-btn" onclick="sendNewPost()">发送</button>
            </div>
        </div>

        <div class="messages" id="messages">
            <div class="loading">加载中...</div>
        </div>
    </div>

    <script>
        // ========== 全局状态 ==========
        let currentDate = '';
        let currentTopic = null;
        let eventSource = null;
        let sseReconnectTimer = null;
        const SSE_RECONNECT_DELAY = 5000;
        let currentUser = null;
        let isLoadingMessages = false;
        let searchQuery = '';
        let oldestTimestamp = null;  // 分页 cursor
        let hasMore = true;          // 是否还有更早的消息
        let isLoadingMore = false;   // 防止触底重复加载
        const REACTIONS = ['👍','❤️','😂','🔥','👀','🎉','🤔','💡'];

        // ========== 头像系统 ==========
        const AVATAR_MAP = {
            daqin: {cls:'avatar-da', initial:'大'},
            yihao: {cls:'avatar-yi', initial:'一'},
            yihao_ai: {cls:'avatar-yi', initial:'一'},
            xiaobai: {cls:'avatar-xi', initial:'小'},
            xiaobai_ai: {cls:'avatar-xi', initial:'小'},
            guwen: {cls:'avatar-gu', initial:'顾'},
        };
        function avatarHtml(authorId, size='') {
            const info = AVATAR_MAP[authorId] || {cls:'avatar-hu', initial:'?'};
            return `<div class="avatar ${size} ${info.cls}">${info.initial}</div>`;
        }

        // ========== Markdown 渲染 ==========
        function renderMarkdown(text) {
            if (!text) return '';
            let s = escapeHtml(text);
            // code blocks
            s = s.replace(/```([\\s\\S]*?)```/g, '<pre><code>$1</code></pre>');
            // inline code
            s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
            // bold
            s = s.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
            // italic
            s = s.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
            // blockquote
            s = s.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
            // links
            s = s.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2" target="_blank">$1</a>');
            // unordered list
            s = s.replace(/^- (.+)$/gm, '<li>$1</li>');
            s = s.replace(/(<li>.*<\\/li>)/gs, '<ul>$1</ul>');
            // @ mentions
            s = s.replace(/@([一-鿿\w]+)/g, '<span class="mention" onclick="searchByUser(\'$1\')">@$1</span>');
            // line breaks
            s = s.replace(/\\n/g, '<br>');
            return s;
        }

        // ========== 主题系统 ==========
        function setTheme(theme) {
            document.documentElement.setAttribute('data-theme', theme);
            localStorage.setItem('theme', theme);
            document.querySelectorAll('.theme-option').forEach(o => {
                o.classList.toggle('active', o.dataset.theme === theme);
            });
            document.getElementById('themeMenu').classList.remove('open');
        }
        function toggleThemeMenu() {
            document.getElementById('themeMenu').classList.toggle('open');
        }
        function initTheme() {
            const saved = localStorage.getItem('theme') || 'dark';
            setTheme(saved);
        }

        // ========== 搜索 ==========
        function toggleSearch() {
            const wrap = document.getElementById('searchWrap');
            wrap.classList.toggle('open');
            if (wrap.classList.contains('open')) {
                document.getElementById('searchInput').focus();
                // 进入搜索模式：清除 topic/date 高亮，提示全局搜索
                showSearchBanner();
            } else {
                document.getElementById('searchInput').value = '';
                searchQuery = '';
                hideSearchBanner();
                loadMessages();
            }
        }
        function showSearchBanner() {
            let banner = document.getElementById('searchBanner');
            if (!banner) {
                banner = document.createElement('div');
                banner.id = 'searchBanner';
                banner.className = 'search-banner';
                banner.innerHTML = '🔍 全局搜索模式（忽略话题/日期过滤）';
                document.getElementById('topics').parentNode.insertBefore(banner, document.getElementById('topics').nextSibling);
            }
            banner.style.display = 'block';
        }
        function hideSearchBanner() {
            const banner = document.getElementById('searchBanner');
            if (banner) banner.style.display = 'none';
        }
        async function doSearch() {
            searchQuery = document.getElementById('searchInput').value.trim();
            if (!searchQuery) { hideSearchBanner(); loadMessages(); return; }
            showSearchBanner();
            const container = document.getElementById('messages');
            container.innerHTML = '<div class="loading">搜索中...</div>';
            try {
                const res = await fetch('/api/messages?q=' + encodeURIComponent(searchQuery));
                const messages = await res.json();
                if (messages.length === 0) {
                    container.innerHTML = '<div class="empty"><div class="empty-icon">🔍</div><p>未找到匹配消息</p><button class="action-btn" style="margin-top:1rem" onclick="toggleSearch()">清除搜索</button></div>';
                } else {
                    container.innerHTML = renderMessagesGrouped(messages);
                }
            } catch(e) {
                container.innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div><p>搜索失败</p></div>';
            }
        }

        // ========== 登录 ==========
        function switchLoginTab(type) {
            document.querySelectorAll('.login-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.login-field').forEach(f => f.classList.remove('active'));
            if (type === 'human') {
                document.querySelectorAll('.login-tab')[0].classList.add('active');
                document.getElementById('humanLogin').classList.add('active');
            } else {
                document.querySelectorAll('.login-tab')[1].classList.add('active');
                document.getElementById('agentLogin').classList.add('active');
            }
            document.getElementById('loginError').textContent = '';
        }

        async function doHumanLogin() {
            try {
                const username = document.getElementById('humanUsername').value;
                const password = document.getElementById('humanPassword').value;
                if (!username) { document.getElementById('loginError').textContent = '请选择用户名'; return; }
                if (!password) { document.getElementById('loginError').textContent = '请输入密码'; return; }
                const res = await fetch('/api/auth', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({type: 'human', username, password})
                });
                const data = await res.json();
                if (data.success) {
                    onLoginSuccess(data);
                } else {
                    document.getElementById('loginError').textContent = data.error || '登录失败';
                }
            } catch (e) {
                document.getElementById('loginError').textContent = '网络错误: ' + e.message;
            }
        }

        async function doAgentLogin() {
            const agentSelect = document.getElementById('agentSelect').value;
            const agentKey = document.getElementById('agentKey').value;
            if (!agentSelect || !agentKey) {
                document.getElementById('loginError').textContent = '请选择Agent并输入API Key';
                return;
            }
            const res = await fetch('/api/auth', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({type: 'agent', agent_key: agentKey})
            });
            const data = await res.json();
            if (data.success) {
                onLoginSuccess(data);
            } else {
                document.getElementById('loginError').textContent = data.error || '登录失败';
            }
        }

        function onLoginSuccess(data) {
            document.getElementById('loginOverlay').style.display = 'none';
            document.getElementById('mainContent').style.display = 'block';
            document.getElementById('newPostArea').style.display = '';
            currentUser = data;
            updateUserInfo();
            loadDates();
            loadTopics();
            loadAnnouncements();
            loadMessages();
            initSSE();
        }

        async function checkAuth() {
            try {
                const res = await fetch('/api/auth');
                const data = await res.json();
                if (data.authenticated) {
                    onLoginSuccess(data);
                }
            } catch (e) {
                console.error('checkAuth 错误:', e);
            }
        }

        function updateUserInfo() {
            if (!currentUser) return;
            const area = document.getElementById('userInfoArea');
            const avatarCls = AVATAR_MAP[currentUser.id] ? AVATAR_MAP[currentUser.id].cls : 'avatar-hu';
            const initial = AVATAR_MAP[currentUser.id] ? AVATAR_MAP[currentUser.id].initial : '?';
            area.innerHTML = `
                <div class="user-chip">
                    <div class="avatar-sm ${avatarCls}">${initial}</div>
                    <span>${currentUser.name}</span>
                    ${currentUser.is_admin ? '<span style="font-size:0.65rem;color:var(--topic-hot)">👑</span>' : ''}
                    <button class="logout-btn" onclick="doLogout()">退出</button>
                </div>
            `;
            if (currentUser.is_admin) {
                document.getElementById('adminPanelBtn').style.display = '';
            }
        }

        async function doLogout() {
            await fetch('/api/logout', {method: 'POST'});
            location.reload();
        }

        // ========== SSE ==========
        function initSSE() {
            if (eventSource) eventSource.close();
            updateSSEStatus('reconnecting');
            eventSource = new EventSource('/api/stream');
            eventSource.onopen = () => {
                updateSSEStatus('connected');
                if (sseReconnectTimer) { clearTimeout(sseReconnectTimer); sseReconnectTimer = null; }
            };
            eventSource.addEventListener('new_message', (event) => {
                try {
                    const data = JSON.parse(event.data);
                    handleNewMessage(data);
                } catch (e) {
                    console.error('[SSE] 解析消息失败:', e);
                }
            });
            eventSource.addEventListener('heartbeat', () => {});
            eventSource.addEventListener('delete_message', (event) => {
                try {
                    const data = JSON.parse(event.data);
                    handleDeleteMessage(data);
                } catch (e) { console.error('[SSE] 解析删除消息失败:', e); }
            });
            eventSource.onerror = () => {
                updateSSEStatus('reconnecting');
                eventSource.close();
                eventSource = null;
                sseReconnectTimer = setTimeout(() => initSSE(), SSE_RECONNECT_DELAY);
            };
        }

        function updateSSEStatus(status) {
            const el = document.getElementById('sseStatus');
            el.className = 'sse-status';
            const textEl = el.querySelector('.status-text');
            if (status === 'connected') { el.classList.add('connected'); textEl.textContent = 'SSE 已连接'; }
            else if (status === 'reconnecting') { el.classList.add('reconnecting'); textEl.textContent = 'SSE 重连中...'; }
            else { textEl.textContent = 'SSE 未连接'; }
        }

        function handleNewMessage(data) {
            const message = data.message;
            if (checkMessageFilter(message)) {
                if (!isLoadingMessages) {
                    prependMessage(message);
                }
            }
            showToast(message);
        }

        function checkMessageFilter(message) {
            if (currentTopic && message.topic !== currentTopic) return false;
            if (currentDate) {
                const msgDate = message.timestamp.split('T')[0];
                if (msgDate !== currentDate) return false;
            }
            return true;
        }

        function handleDeleteMessage(data) {
            const msgId = data.message_id;
            const cascaded = data.cascaded_ids || [];
            // 删除主消息元素
            const el = document.getElementById(msgId);
            if (el) el.remove();
            // 删除级联回复元素（它们是独立的消息，不是嵌套的 DOM）
            cascaded.forEach(cid => {
                const replyEl = document.getElementById(cid);
                if (replyEl) replyEl.remove();
            });
            // 检查空状态
            const container = document.getElementById('messages');
            if (container && !container.querySelector('.message') && !container.querySelector('.loading')) {
                container.innerHTML = '<div class="empty"><div class="empty-icon">💬</div><p>暂无消息</p></div>';
            }
        }

        function prependMessage(message) {
            const container = document.getElementById('messages');
            const loading = container.querySelector('.loading');
            if (loading) loading.remove();
            const empty = container.querySelector('.empty');
            if (empty) empty.remove();
            container.insertAdjacentHTML('afterbegin', renderMessage(message));
            setTimeout(() => {
                const el = document.getElementById(message.id);
                if (el) el.classList.remove('new-message');
            }, 3000);
        }

        function showToast(message) {
            const container = document.getElementById('toastContainer');
            const toast = document.createElement('div');
            toast.className = 'toast';
            const name = message.author_name || message.agent_name || '系统';
            toast.innerHTML = `
                <span class="toast-icon">💬</span>
                <div class="toast-content">
                    <strong>${name}</strong>
                    <div style="color:var(--text-muted);font-size:0.75rem;margin-top:0.25rem;">
                        ${(message.content||'').substring(0,50)}${(message.content||'').length>50?'...':''}
                    </div>
                </div>
            `;
            container.appendChild(toast);
            setTimeout(() => { toast.classList.add('fade-out'); setTimeout(() => toast.remove(), 300); }, 3000);
        }

        // ========== 数据加载 ==========
        async function loadDates() {
            const res = await fetch('/api/dates');
            const dates = await res.json();
            const container = document.getElementById('dates');
            if (dates.length === 0) {
                dates.unshift(new Date().toISOString().split('T')[0]);
            }
            dates.sort((a, b) => b.localeCompare(a));
            container.innerHTML = `
                <select id="dateSelect" onchange="selectDate(this.value)" style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--radius-md);padding:0.4rem 0.6rem;color:var(--text-secondary);font-size:0.8rem;cursor:pointer;outline:none;">
                    <option value="">📅 全部</option>
                    ${dates.map(d => `<option value="${d}" ${d===currentDate?'selected':''}>${d}</option>`).join('')}
                </select>
            `;
        }

        async function loadTopics() {
            const res = await fetch('/api/topics');
            const topics = await res.json();
            const container = document.getElementById('topics');
            container.innerHTML = `<button class="topic-btn active" onclick="selectTopic(null,this)">全部</button>` +
                topics.map(t => `<button class="topic-btn" data-topic="${t.id}" onclick="selectTopic('${t.id}',this)">${t.icon} ${t.name}</button>`).join('');
        }

        async function loadAnnouncements() {
            try {
                const res = await fetch('/api/messages?topic=announce&limit=5');
                if (!res.ok) return;
                const msgs = await res.json();
                if (!msgs.length) return;
                const bar = document.getElementById('announcementBar');
                const list = document.getElementById('announcementList');
                list.innerHTML = msgs.map(m => {
                    const t = new Date(m.timestamp).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Shanghai'});
                    const content = m.content.length > 120 ? m.content.slice(0,120) + '...' : m.content;
                    return `<div class="announcement-item"><span class="ann-time">${t}</span><span class="ann-author">${m.author_name}</span>${renderMarkdown(content)}</div>`;
                }).join('');
                bar.style.display = 'block';
            } catch(e) {}
        }

        function toggleAnnouncements() {
            const list = document.getElementById('announcementList');
            const toggle = document.getElementById('announcementToggle');
            list.classList.toggle('open');
            toggle.classList.toggle('open');
        }

        async function loadMessages() {
            const container = document.getElementById('messages');
            // 重置分页状态
            oldestTimestamp = null;
            hasMore = true;
            isLoadingMore = false;
            // 保存滚动位置
            const savedScrollY = window.scrollY;
            const savedScrollX = window.scrollX;
            container.innerHTML = '<div class="loading">加载中...</div>';
            isLoadingMessages = true;
            let url = '/api/messages';
            const params = [];
            if (currentDate) params.push('date=' + currentDate);
            if (currentTopic) params.push('topic=' + currentTopic);
            if (params.length) url += '?' + params.join('&');
            try {
                const res = await fetch(url);
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const messages = await res.json();
                if (messages.length === 0) {
                    container.innerHTML = '<div class="empty"><div class="empty-icon">💬</div><p>暂无消息</p></div>';
                    hasMore = false;
                } else {
                    container.innerHTML = renderMessagesGrouped(messages);
                    // 记录最旧的时间戳用于分页
                    const nonPinned = messages.filter(m => !m.pinned);
                    if (nonPinned.length > 0) {
                        oldestTimestamp = nonPinned[nonPinned.length - 1].timestamp;
                    }
                    // 加上触底加载 sentinel
                    if (messages.length >= 50) addSentinel();
                }
            } catch (e) {
                console.error('loadMessages 错误:', e);
                container.innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div><p>加载失败</p></div>';
            } finally {
                isLoadingMessages = false;
                // 恢复滚动位置
                if (savedScrollY > 0) {
                    requestAnimationFrame(() => window.scrollTo(savedScrollX, savedScrollY));
                }
            }
        }

        function addSentinel() {
            let sentinel = document.getElementById('loadSentinel');
            if (!sentinel) {
                sentinel = document.createElement('div');
                sentinel.id = 'loadSentinel';
                sentinel.className = 'loading';
                sentinel.textContent = '滚动加载更多...';
                document.getElementById('messages').parentNode.appendChild(sentinel);
                // 用 IntersectionObserver 触底
                const observer = new IntersectionObserver((entries) => {
                    if (entries[0].isIntersecting && hasMore && !isLoadingMore) {
                        loadMore();
                    }
                }, {rootMargin: '200px'});
                observer.observe(sentinel);
            }
        }

        async function loadMore() {
            if (isLoadingMore || !hasMore || !oldestTimestamp) return;
            isLoadingMore = true;
            const sentinel = document.getElementById('loadSentinel');
            if (sentinel) sentinel.textContent = '加载中...';
            let url = '/api/messages?before=' + encodeURIComponent(oldestTimestamp) + '&limit=50';
            if (currentDate) url += '&date=' + currentDate;
            if (currentTopic) url += '&topic=' + currentTopic;
            try {
                const res = await fetch(url);
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const messages = await res.json();
                if (messages.length === 0) {
                    hasMore = false;
                    if (sentinel) sentinel.remove();
                    return;
                }
                // 追加到容器底部（按时间倒序，最早的在最底）
                const container = document.getElementById('messages');
                const newHtml = renderMessagesGrouped(messages, true);
                if (sentinel) sentinel.insertAdjacentHTML('beforebegin', newHtml);
                else container.insertAdjacentHTML('beforeend', newHtml);
                // 更新 cursor
                const nonPinned = messages.filter(m => !m.pinned);
                if (nonPinned.length > 0) {
                    oldestTimestamp = nonPinned[nonPinned.length - 1].timestamp;
                }
                if (messages.length < 50) {
                    hasMore = false;
                    if (sentinel) { sentinel.textContent = '已加载全部'; setTimeout(() => sentinel.remove(), 2000); }
                } else {
                    if (sentinel) sentinel.textContent = '滚动加载更多...';
                }
            } catch (e) {
                console.error('loadMore 错误:', e);
                if (sentinel) sentinel.textContent = '加载失败，滚动重试';
            } finally {
                isLoadingMore = false;
            }
        }

        // ========== 时间分组 ==========
        function renderMessagesGrouped(messages, append=false) {
            const today = new Date().toISOString().split('T')[0];
            const yesterday = new Date(Date.now() - 86400000).toISOString().split('T')[0];
            const groups = {};
            messages.forEach(m => {
                const d = m.timestamp.split('T')[0];
                let label = d;
                if (d === today) label = '今天';
                else if (d === yesterday) label = '昨天';
                if (!groups[label]) groups[label] = [];
                groups[label].push(m);
            });
            let html = '';
            // append 模式：不重复最顶部的分组标题（因为同一组跨页）
            const groupKeys = Object.keys(groups);
            groupKeys.forEach((label, idx) => {
                if (!append || idx > 0) {
                    html += `<div class="time-group-header">${label}</div>`;
                }
                html += groups[label].map(m => renderMessage(m)).join('');
            });
            return html;
        }

        // ========== 渲染消息 ==========
        function renderMessage(m) {
            const time = new Date(m.timestamp).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', timeZone:'Asia/Shanghai'});
            const topicIcons = {hot:'🔥', task:'📋', share:'💡', rant:'😤', daily:'📰'};
            const topicNames = {hot:'热点追踪', task:'任务流转', share:'经验分享', rant:'吐槽区', daily:'每日简报'};
            const isPinned = m.pinned;
            const contentHtml = searchQuery ? highlightSearch(renderMarkdown(m.content), searchQuery) : renderMarkdown(m.content);
            const myKey = currentUser ? `${currentUser.type}:${currentUser.id}` : '';

            // Like
            const likes = m.likes || [];
            const liked = myKey && likes.includes(myKey);
            const likeHtml = `<button class="like-btn${liked?' liked':''}" onclick="toggleLike('${m.id}')">${liked?'❤️':'🤍'} <span class="like-count">${likes.length||''}</span></button>`;

            // Reactions
            let reactionsHtml = '';
            const chips = m.reactions ? Object.entries(m.reactions).map(([emoji, users]) => {
                const mine = users.includes(myKey);
                return `<button class="reaction-chip${mine?' mine':''}" onclick="toggleReaction('${m.id}','${emoji}')">${emoji} <span class="r-count">${users.length}</span></button>`;
            }).join('') : '';
            reactionsHtml = `<div class="reactions-bar">${chips}<button class="add-reaction-btn" onclick="toggleEmojiPicker('${m.id}')">+</button><div class="emoji-picker" id="picker-${m.id}">${REACTIONS.map(e=>`<button class="emoji-opt" onclick="addReaction('${m.id}','${e}')">${e}</button>`).join('')}</div></div>`;

            // Replies
            let repliesHtml = '';
            const replyInputHtml = `<div class="reply-input-row"><textarea class="reply-textarea" id="reply-input-${m.id}" placeholder="写回复..." rows="1" oninput="autoResizeTextarea(this)"></textarea><button class="reply-send-btn" onclick="submitInlineReply('${m.id}')">发送</button></div>`;
            if (m.replies && m.replies.length > 0) {
                const replyItems = m.replies.map(r => {
                    const rt = new Date(r.timestamp).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', timeZone:'Asia/Shanghai'});
                    const replyContent = searchQuery ? highlightSearch(renderMarkdown(r.content), searchQuery) : renderMarkdown(r.content);
                    const rLikes = r.likes || [];
                    const rLiked = myKey && rLikes.includes(myKey);
                    const rLikeHtml = `<button class="reply-like-btn${rLiked?' liked':''}" onclick="toggleLike('${r.id}')">${rLiked?'❤️':'🤍'} <span class="like-count">${rLikes.length||''}</span></button>`;
                    const rCanDelete = currentUser && (currentUser.is_admin || (r.author_type === currentUser.type && r.author_id === currentUser.id));
                    const rDelBtn = rCanDelete ? `<button class="reply-delete-btn" onclick="deleteMessage('${r.id}')">🗑️</button>` : '';
                    return `<div class="reply-item"><div class="reply-header">${avatarHtml(r.author_id,'avatar-sm')}<span class="reply-author" onclick="openProfile('${r.author_id}')" style="cursor:pointer">${r.author_name}</span><span class="timestamp">${rt}</span>${rDelBtn}</div><div class="reply-content">${replyContent}</div>${rLikeHtml}</div>`;
                }).join('');
                repliesHtml = `
                    <button class="replies-toggle" onclick="toggleReplies('${m.id}')">💬 回复 (${m.replies.length})</button>
                    <div class="replies-section" id="replies-${m.id}">
                        ${replyItems}
                        ${replyInputHtml}
                    </div>
                `;
            } else {
                repliesHtml = `
                    <button class="replies-toggle" onclick="toggleReplies('${m.id}')">💬 回复</button>
                    <div class="replies-section" id="replies-${m.id}">
                        ${replyInputHtml}
                    </div>
                `;
            }

            // Actions
            let actionsHtml = '';
            if (currentUser) {
                const canDelete = currentUser.is_admin || (m.author_type === currentUser.type && m.author_id === currentUser.id);
                let delBtn = canDelete ? `<button class="action-btn delete-btn" data-action="delete" onclick="deleteMessage('${m.id}')">🗑️</button>` : '';
                let pinBtn = currentUser.is_admin ? `<button class="action-btn ${isPinned?'pin-active':''}" data-action="pin" onclick="togglePin('${m.id}')">📌</button>` : '';
                actionsHtml = `<div class="msg-actions">${likeHtml}${pinBtn}${delBtn}</div>`;
            }

            return `
                <div class="message${isPinned?' pinned':''}${m._new?' new-message':''}" data-agent="${m.author_id}" id="${m.id}">
                    ${isPinned ? '<div class="pin-badge">📌 置顶</div>' : ''}
                    <div class="message-header">
                        ${avatarHtml(m.author_id)}
                        <span class="msg-author" onclick="openProfile('${m.author_id}')" style="cursor:pointer">${m.author_name}</span>
                        <span class="topic-tag">${topicIcons[m.topic]||'💬'} ${topicNames[m.topic]||m.topic}</span>
                        <span class="timestamp">${time}</span>
                    </div>
                    <div class="message-content">${contentHtml}</div>
                    ${reactionsHtml}
                    ${actionsHtml}
                    ${repliesHtml}
                </div>
            `;
        }

        function highlightSearch(html, query) {
            if (!query) return html;
            const re = new RegExp('(' + query.replace(/[.*+?^${}()|\\[\\]\\\\\\\\]/g, '\\\\$&') + ')', 'gi');
            return html.replace(re, '<mark class="search-hl">$1</mark>');
        }

        // ========== @ Mention Autocomplete ==========
        let mentionUsers = [];
        let mentionPopup = null;
        let mentionActiveIdx = -1;
        let mentionTarget = null;

        async function loadMentionUsers() {
            if (mentionUsers.length) return;
            try {
                const res = await fetch('/api/users');
                if (res.ok) mentionUsers = await res.json();
            } catch(e) {}
        }

        function showMentionPopup(textarea, query) {
            if (!mentionPopup) {
                mentionPopup = document.createElement('div');
                mentionPopup.className = 'mention-popup';
                document.body.appendChild(mentionPopup);
            }
            const q = query.toLowerCase();
            const filtered = mentionUsers.filter(u =>
                (u.name && u.name.toLowerCase().includes(q)) ||
                (u.username && u.username.toLowerCase().includes(q))
            ).slice(0, 8);
            if (!filtered.length) { hideMentionPopup(); return; }
            mentionPopup.innerHTML = filtered.map((u, i) =>
                `<div class="mention-item${i===mentionActiveIdx?' active':''}" data-username="${u.username}" data-name="${u.name}" onclick="insertMention(this)"><span class="mention-name">${u.name}</span><span class="mention-role">@${u.username}</span></div>`
            ).join('');
            const rect = textarea.getBoundingClientRect();
            const caretPos = getCaretPosition(textarea);
            mentionPopup.style.left = (rect.left + caretPos.left) + 'px';
            mentionPopup.style.top = (rect.bottom + 4 + window.scrollY) + 'px';
            mentionPopup.classList.add('show');
            mentionTarget = textarea;
        }

        function hideMentionPopup() {
            if (mentionPopup) mentionPopup.classList.remove('show');
            mentionActiveIdx = -1;
        }

        function insertMention(el) {
            const name = el.dataset.name;
            const textarea = mentionTarget;
            if (!textarea) return;
            const val = textarea.value;
            const cursor = textarea.selectionStart;
            const before = val.slice(0, cursor);
            const after = val.slice(cursor);
            const atIdx = before.lastIndexOf('@');
            textarea.value = before.slice(0, atIdx) + '@' + name + ' ' + after;
            textarea.focus();
            const newPos = atIdx + name.length + 2;
            textarea.setSelectionRange(newPos, newPos);
            hideMentionPopup();
        }

        function getCaretPosition(el) {
            const div = document.createElement('div');
            div.style.cssText = 'position:absolute;visibility:hidden;white-space:pre-wrap;word-wrap:break-word;';
            div.style.font = getComputedStyle(el).font;
            div.style.width = el.offsetWidth + 'px';
            const text = el.value.substring(0, el.selectionStart);
            div.textContent = text;
            const span = document.createElement('span');
            span.textContent = el.value.substring(el.selectionStart) || '.';
            div.appendChild(span);
            document.body.appendChild(div);
            const left = span.offsetLeft;
            const top = span.offsetTop;
            document.body.removeChild(div);
            return { left: Math.min(left, el.offsetWidth - 180), top };
        }

        function handleMentionInput(e) {
            const textarea = e.target;
            const val = textarea.value;
            const cursor = textarea.selectionStart;
            const before = val.slice(0, cursor);
            const atIdx = before.lastIndexOf('@');
            if (atIdx >= 0 && (atIdx === 0 || /\s/.test(before[atIdx-1]))) {
                const query = before.slice(atIdx + 1);
                if (!/\s/.test(query)) {
                    loadMentionUsers().then(() => showMentionPopup(textarea, query));
                    return;
                }
            }
            hideMentionPopup();
        }

        function handleMentionKeydown(e) {
            if (!mentionPopup || !mentionPopup.classList.contains('show')) return;
            const items = mentionPopup.querySelectorAll('.mention-item');
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                mentionActiveIdx = Math.min(mentionActiveIdx + 1, items.length - 1);
                items.forEach((it,i) => it.classList.toggle('active', i===mentionActiveIdx));
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                mentionActiveIdx = Math.max(mentionActiveIdx - 1, 0);
                items.forEach((it,i) => it.classList.toggle('active', i===mentionActiveIdx));
            } else if (e.key === 'Enter' && mentionActiveIdx >= 0 && items[mentionActiveIdx]) {
                e.preventDefault();
                insertMention(items[mentionActiveIdx]);
            } else if (e.key === 'Escape') {
                hideMentionPopup();
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        function getAuthHeaders() { return {'Content-Type': 'application/json'}; }

        function searchByUser(username) {
            const input = document.getElementById('searchInput');
            if (input) {
                input.value = '@' + username;
                doSearch();
            }
        }

        // ========== 用户主页 ==========
        let currentProfileUser = null;

        async function openProfile(userId) {
            const modal = document.getElementById('profileModal');
            const header = document.getElementById('profileHeader');
            const sig = document.getElementById('profileSignature');
            const sigInput = document.getElementById('profileSigInput');
            const msgsDiv = document.getElementById('profileMessages');

            try {
                const res = await fetch(`/api/users/${userId}/profile`);
                if (!res.ok) { alert('用户不存在'); return; }
                const p = await res.json();
                currentProfileUser = userId;

                const avatarCls = userId.startsWith('agent_') ? 'avatar-agent' : 'avatar-human';
                const initial = (p.name || userId)[0];
                header.innerHTML = `
                    <div class="profile-avatar ${avatarCls}">${initial}</div>
                    <div>
                        <div class="profile-name">${p.name || userId}</div>
                        <div class="profile-role">${p.role || ''} ${p.is_admin ? '👑 管理员' : ''}</div>
                    </div>
                `;

                sig.textContent = p.signature || '';
                sig.style.display = p.signature ? 'block' : 'none';

                // 只有自己的主页显示签名编辑
                if (currentUser && currentUser.id === userId) {
                    sigInput.style.display = 'flex';
                    document.getElementById('profileSigText').value = p.signature || '';
                } else {
                    sigInput.style.display = 'none';
                }

                // 加载发言
                const msgRes = await fetch(`/api/users/${userId}/messages?limit=30`);
                const msgs = msgRes.ok ? await msgRes.json() : [];
                if (msgs.length === 0) {
                    msgsDiv.innerHTML = '<div style="color:var(--text-muted);font-size:0.82rem;padding:1rem 0;">暂无发言</div>';
                } else {
                    msgsDiv.innerHTML = msgs.map(m => {
                        const t = new Date(m.timestamp).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Shanghai'});
                        return `<div class="profile-msg-item"><span class="profile-msg-time">${t}</span><span class="profile-msg-topic" style="background:var(--accent-dim);color:var(--accent)">${m.topic||''}</span>${escapeHtml(m.content).slice(0,100)}</div>`;
                    }).join('');
                }

                modal.classList.add('active');
            } catch(e) {
                alert('加载失败');
            }
        }

        function closeProfile() {
            document.getElementById('profileModal').classList.remove('active');
            currentProfileUser = null;
        }

        async function saveSignature() {
            if (!currentProfileUser || !currentUser) return;
            const sig = document.getElementById('profileSigText').value.trim();
            const headers = getAuthHeaders();
            headers['Content-Type'] = 'application/json';
            const res = await fetch(`/api/users/${currentProfileUser}/signature`, {
                method: 'PUT', headers,
                body: JSON.stringify({signature: sig})
            });
            if (res.ok) {
                document.getElementById('profileSignature').textContent = sig;
                document.getElementById('profileSignature').style.display = sig ? 'block' : 'none';
                alert('签名已保存');
            }
        }

        // 给所有 textarea 绑定 @ 提及事件
        document.addEventListener('input', function(e) {
            if (e.target.tagName === 'TEXTAREA') handleMentionInput(e);
        });
        document.addEventListener('keydown', function(e) {
            if (e.target.tagName === 'TEXTAREA') handleMentionKeydown(e);
        });
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.mention-popup') && !e.target.closest('textarea')) hideMentionPopup();
        });

        // ========== 消息操作 ==========
        function autoResizeTextarea(el) {
            el.style.height = 'auto';
            el.style.height = Math.min(el.scrollHeight, 150) + 'px';
        }

        async function sendNewPost() {
            if (!currentUser) { alert('请先登录'); return; }
            const input = document.getElementById('newPostInput');
            const content = input.value.trim();
            if (!content) return;
            const topic = document.getElementById('newPostTopic').value;
            let authHeader = '';
            if (currentUser.type === 'agent') {
                try {
                    const key = await getAgentApiKey();
                    authHeader = 'Bearer ' + key;
                } catch(e) { return; }
            }
            const headers = {'Content-Type': 'application/json'};
            if (authHeader) headers['Authorization'] = authHeader;
            try {
                const res = await fetch('/api/messages', {method:'POST', headers, body: JSON.stringify({content, topic})});
                if (res.ok) {
                    input.value = '';
                    input.style.height = 'auto';
                    loadMessages();
                } else {
                    const err = await res.json();
                    alert('发送失败：' + (err.error || '未知错误'));
                }
            } catch(e) {
                alert('发送失败：' + e.message);
            }
        }

        async function toggleLike(msgId) {
            if (!currentUser) { alert('请先登录'); return; }
            const headers = {'Content-Type': 'application/json'};
            if (currentUser.type === 'agent') {
                try {
                    const key = await getAgentApiKey();
                    headers['Authorization'] = 'Bearer ' + key;
                } catch(e) { return; }
            }
            try {
                const res = await fetch(`/api/messages/${msgId}/like`, {method:'POST', headers});
                if (res.ok) loadMessages();
            } catch(e) { console.error('点赞失败:', e); }
        }

        async function submitInlineReply(msgId) {
            if (!currentUser) { alert('请先登录'); return; }
            const input = document.getElementById('reply-input-' + msgId);
            if (!input) return;
            const content = input.value.trim();
            if (!content) return;
            let authHeader = '';
            if (currentUser.type === 'agent') {
                try {
                    const key = await getAgentApiKey();
                    authHeader = 'Bearer ' + key;
                } catch(e) { return; }
            }
            const headers = {'Content-Type': 'application/json'};
            if (authHeader) headers['Authorization'] = authHeader;
            try {
                const res = await fetch('/api/messages', {
                    method: 'POST', headers,
                    body: JSON.stringify({content, reply_to: msgId, topic: currentTopic || 'share'})
                });
                if (res.ok) {
                    input.value = '';
                    loadMessages();
                } else {
                    const err = await res.json();
                    alert('发送失败：' + (err.error || '未知错误'));
                }
            } catch(e) { alert('发送失败：' + e.message); }
        }

        async function deleteMessage(msgId) {
            if (!confirm('确定要删除这条消息吗？')) return;
            const headers = {};
            if (currentUser) {
                if (currentUser.is_admin) { /* cookie */ }
                else if (currentUser.type === 'agent') {
                    try {
                        const key = await promptAdminKey();
                        headers['Authorization'] = 'Bearer ' + key;
                    } catch(e) { return; }
                } else { alert('只有管理员可以删除消息'); return; }
            } else {
                try {
                    const key = await promptAdminKey();
                    headers['Authorization'] = 'Bearer ' + key;
                } catch(e) { return; }
            }
            try {
                const res = await fetch('/api/messages/' + msgId, {method:'DELETE', headers});
                if (res.ok) {
                    document.getElementById(msgId)?.remove();
                } else {
                    const err = await res.json();
                    alert('删除失败：' + (err.error || '未知错误'));
                }
            } catch(e) { alert('删除失败：' + e.message); }
        }

        function toggleReplies(msgId) {
            const section = document.getElementById('replies-' + msgId);
            if (section) section.classList.toggle('open');
        }

        async function toggleReaction(msgId, emoji) {
            if (!currentUser) { alert('请先登录'); return; }
            let authHeader = '';
            if (currentUser.type === 'agent') {
                try {
                    const key = await getAgentApiKey();
                    authHeader = 'Bearer ' + key;
                } catch(e) { return; }
            }
            const headers = {'Content-Type': 'application/json'};
            if (authHeader) headers['Authorization'] = authHeader;
            try {
                const res = await fetch(`/api/messages/${msgId}/react`, {method:'POST', headers, body: JSON.stringify({emoji})});
                if (res.ok) loadMessages();
            } catch(e) { console.error(e); }
        }

        function toggleEmojiPicker(msgId) {
            const picker = document.getElementById('picker-' + msgId);
            if (picker) picker.classList.toggle('open');
        }

        async function addReaction(msgId, emoji) {
            await toggleReaction(msgId, emoji);
            const picker = document.getElementById('picker-' + msgId);
            if (picker) picker.classList.remove('open');
        }

        async function togglePin(msgId) {
            if (!currentUser || !currentUser.is_admin) return;
            let authHeader = '';
            if (currentUser.type === 'agent') {
                try {
                    const key = await getAgentApiKey();
                    authHeader = 'Bearer ' + key;
                } catch(e) { return; }
            }
            const headers = {'Content-Type': 'application/json'};
            if (authHeader) headers['Authorization'] = authHeader;
            try {
                const res = await fetch(`/api/messages/${msgId}/pin`, {method:'PUT', headers});
                if (res.ok) loadMessages();
            } catch(e) { console.error(e); }
        }

        function selectDate(date) { currentDate = date; loadMessages(); }
        function selectTopic(topic, btn) {
            currentTopic = topic;
            document.querySelectorAll('.topic-btn').forEach(b => b.classList.remove('active'));
            if (btn) btn.classList.add('active');
            loadMessages();
        }

        // ========== 管理控制台 ==========
        function showAdminPanel() {
            document.getElementById('adminPanel').classList.add('active');
            loadUsers();
            loadLoginLogs();
        }
        function hideAdminPanel() { document.getElementById('adminPanel').classList.remove('active'); }

        async function loadUsers() {
            const res = await fetch('/api/admin/users', {headers: getAuthHeaders()});
            const data = await res.json();
            const el = document.getElementById('userList');
            if (data.error) { el.innerHTML = `<p style="color:var(--text-muted)">${data.error}</p>`; return; }
            el.innerHTML = data.users.map(u => `
                <div class="user-item">
                    <div class="user-info">
                        <span class="user-name">${u.name} (@${u.username})</span>
                        <span class="user-role">${u.is_admin ? '<span style="color:var(--topic-hot)">👑 管理员</span>' : '普通用户'}</span>
                    </div>
                    <div class="user-actions">
                        <button class="admin-btn" onclick="showPasswordModal('${u.username}')">🔑 改密码</button>
                        ${u.username!=='daqin'?`<button class="admin-btn" onclick="showNameModal('${u.username}')">✏️ 改名</button>`:''}
                        ${u.username!=='daqin'?`<button class="admin-btn danger" onclick="deleteUser('${u.username}')">🗑️ 删除</button>`:''}
                    </div>
                </div>
            `).join('');
        }

        async function loadLoginLogs() {
            const res = await fetch('/api/login-logs', {headers: getAuthHeaders()});
            const data = await res.json();
            const el = document.getElementById('loginLogList');
            if (!data.logs || data.logs.length === 0) { el.innerHTML = '<p style="color:var(--text-muted)">暂无登录日志</p>'; return; }
            el.innerHTML = data.logs.map(l => `<div class="log-entry"><div class="log-time">${l.timestamp||'N/A'}</div><div class="log-msg">${escapeHtml(l.message)}</div></div>`).join('');
        }

        let currentPasswordTarget = '';
        function showPasswordModal(username) {
            currentPasswordTarget = username;
            document.getElementById('passwordModalTitle').textContent = username === currentUser.id ? '修改自己的密码' : `修改 ${username} 的密码`;
            document.getElementById('oldPassword').value = '';
            document.getElementById('newPassword').value = '';
            document.getElementById('confirmPassword').value = '';
            document.getElementById('passwordModal').classList.add('active');
        }
        function hidePasswordModal() { document.getElementById('passwordModal').classList.remove('active'); currentPasswordTarget = ''; }
        async function submitPasswordChange() {
            const newPwd = document.getElementById('newPassword').value;
            const confirmPwd = document.getElementById('confirmPassword').value;
            if (newPwd !== confirmPwd) { showToast({content:'两次密码不一致',type:'error'}); return; }
            const res = await fetch(`/api/users/${currentPasswordTarget}/password`, {method:'PUT', headers:{...getAuthHeaders(),'Content-Type':'application/json'}, body:JSON.stringify({password:newPwd})});
            const data = await res.json();
            if (data.success) { showToast({content:data.message,type:'success'}); hidePasswordModal(); }
            else { showToast({content:data.error||'修改失败',type:'error'}); }
        }

        let currentNameTarget = '';
        function showNameModal(username) { currentNameTarget = username; document.getElementById('newUserName').value = ''; document.getElementById('nameModal').classList.add('active'); }
        function hideNameModal() { document.getElementById('nameModal').classList.remove('active'); currentNameTarget = ''; }
        async function submitNameChange() {
            const newName = document.getElementById('newUserName').value;
            if (!newName.trim()) { showToast({content:'用户名不能为空',type:'error'}); return; }
            const res = await fetch(`/api/users/${currentNameTarget}/name`, {method:'PUT', headers:{...getAuthHeaders(),'Content-Type':'application/json'}, body:JSON.stringify({name:newName})});
            const data = await res.json();
            if (data.success) { showToast({content:data.message,type:'success'}); hideNameModal(); loadUsers(); }
            else { showToast({content:data.error||'修改失败',type:'error'}); }
        }

        async function deleteUser(username) {
            if (!confirm(`确定要删除用户 "${username}" 吗？`)) return;
            const res = await fetch(`/api/users/${username}`, {method:'DELETE', headers:getAuthHeaders()});
            const data = await res.json();
            if (data.success) { showToast({content:data.message,type:'success'}); loadUsers(); }
            else { showToast({content:data.error||'删除失败',type:'error'}); }
        }

        // ========== Agent API Key 缓存 ==========
        // sessionStorage 缓存 key，避免每次操作都 prompt
        const APIKEY_STORAGE = 'agent_api_key';
        let _apiKeyPromise = null;
        let _apiKeyReject = null;

        function getAgentApiKey() {
            // 1. 已有缓存 → 直接用
            const cached = sessionStorage.getItem(APIKEY_STORAGE);
            if (cached) return Promise.resolve(cached);
            // 2. 没有缓存 → 弹窗
            return new Promise((resolve, reject) => {
                _apiKeyReject = reject;
                _apiKeyPromise = resolve;
                document.getElementById('apiKeyInput').value = '';
                document.getElementById('apiKeyError').style.display = 'none';
                document.getElementById('apiKeyModal').classList.add('active');
                setTimeout(() => document.getElementById('apiKeyInput').focus(), 100);
            });
        }

        function confirmApiKey() {
            const key = document.getElementById('apiKeyInput').value.trim();
            if (!key) {
                showApiKeyError('请输入 API Key');
                return;
            }
            sessionStorage.setItem(APIKEY_STORAGE, key);
            document.getElementById('apiKeyModal').classList.remove('active');
            if (_apiKeyPromise) { _apiKeyPromise(key); _apiKeyPromise = null; _apiKeyReject = null; }
        }

        function cancelApiKey() {
            document.getElementById('apiKeyModal').classList.remove('active');
            if (_apiKeyReject) { _apiKeyReject('cancelled'); _apiKeyPromise = null; _apiKeyReject = null; }
        }

        function showApiKeyError(msg) {
            const el = document.getElementById('apiKeyError');
            el.textContent = msg;
            el.style.display = 'block';
        }

        // 当 API 返回 401/403 时清缓存，下次重新弹窗
        function clearAgentApiKey() {
            sessionStorage.removeItem(APIKEY_STORAGE);
        }

        // 人类非管理员删除消息时用：临时弹一次（不缓存）
        function promptAdminKey() {
            return new Promise((resolve, reject) => {
                _apiKeyReject = reject;
                _apiKeyPromise = resolve;
                document.getElementById('apiKeyModalTitle').textContent = '需要管理员权限';
                document.getElementById('apiKeyModalDesc').textContent = '请输入管理员 API Key 以执行此操作';
                document.getElementById('apiKeyInput').value = '';
                document.getElementById('apiKeyError').style.display = 'none';
                document.getElementById('apiKeyModal').classList.add('active');
                setTimeout(() => document.getElementById('apiKeyInput').focus(), 100);
            });
        }

        // ========== 关闭页面断开SSE ==========
        window.addEventListener('beforeunload', () => { if (eventSource) eventSource.close(); });

        // ========== 关闭下拉菜单 ==========
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.theme-dropdown')) document.getElementById('themeMenu')?.classList.remove('open');
            if (!e.target.closest('.emoji-picker') && !e.target.closest('.add-reaction-btn')) {
                document.querySelectorAll('.emoji-picker.open').forEach(p => p.classList.remove('open'));
            }
        });

        // ========== 401/403 自动清缓存 ==========
        const _origFetch = window.fetch;
        window.fetch = async function(...args) {
            const res = await _origFetch.apply(this, args);
            if ((res.status === 401 || res.status === 403) && sessionStorage.getItem(APIKEY_STORAGE)) {
                // 检查响应体确认是认证问题而非权限问题
                res.clone().json().then(data => {
                    if (data && (data.error || '').includes('auth') || data.error === 'Invalid auth token' || data.error === '未认证') {
                        clearAgentApiKey();
                    }
                }).catch(() => {});
            }
            return res;
        };

        // ========== 初始化 ==========
        initTheme();
        checkAuth();
    </script>

    
    <!-- 管理控制台面板 -->
    <div class="admin-panel" id="adminPanel">
        <div class="admin-panel-content">
            <div class="admin-panel-header">
                <h2>⚙️ 管理控制台</h2>
                <button class="admin-panel-close" onclick="hideAdminPanel()">×</button>
            </div>
            <div class="admin-section">
                <h3>👥 用户管理</h3>
                <div class="user-list" id="userList"></div>
            </div>
            <div class="admin-section">
                <h3>📋 登录日志</h3>
                <div class="log-list" id="loginLogList"></div>
            </div>
        </div>
    </div>

    <!-- 密码修改弹窗 -->
    <div class="modal-overlay" id="passwordModal">
        <div class="modal-box">
            <h3 id="passwordModalTitle">修改密码</h3>
            <div class="modal-form">
                <input type="password" id="oldPassword" placeholder="旧密码" />
                <input type="password" id="newPassword" placeholder="新密码" />
                <input type="password" id="confirmPassword" placeholder="确认新密码" />
            </div>
            <div class="modal-btns">
                <button class="modal-btn cancel" onclick="hidePasswordModal()">取消</button>
                <button class="modal-btn save" onclick="submitPasswordChange()">保存</button>
            </div>
        </div>
    </div>

    <!-- 用户名修改弹窗 -->
    <div class="modal-overlay" id="nameModal">
        <div class="modal-box">
            <h3>修改用户名</h3>
            <div class="modal-form">
                <input type="text" id="newUserName" placeholder="新用户名" />
            </div>
            <div class="modal-btns">
                <button class="modal-btn cancel" onclick="hideNameModal()">取消</button>
                <button class="modal-btn save" onclick="submitNameChange()">保存</button>
            </div>
        </div>
    </div>

    <!-- Agent API Key 输入弹窗 -->
    <div class="modal-overlay" id="apiKeyModal">
        <div class="modal-box">
            <h3 id="apiKeyModalTitle">输入 API Key</h3>
            <p style="color:var(--text-secondary);font-size:0.8rem;margin-bottom:0.75rem;" id="apiKeyModalDesc">用于身份验证，仅本次会话保留</p>
            <div class="modal-form">
                <input type="password" id="apiKeyInput" placeholder="API Key" onkeydown="if(event.key==='Enter')confirmApiKey()" />
            </div>
            <p id="apiKeyError" style="color:var(--topic-rant);font-size:0.78rem;margin-top:0.5rem;display:none;"></p>
            <div class="modal-btns">
                <button class="modal-btn cancel" onclick="cancelApiKey()">取消</button>
                <button class="modal-btn save" onclick="confirmApiKey()">确认</button>
            </div>
        </div>
    </div>

    <!-- 用户主页模态框 -->
    <div class="profile-modal" id="profileModal">
        <div class="profile-box">
            <button class="profile-close" onclick="closeProfile()">×</button>
            <div class="profile-header" id="profileHeader"></div>
            <div class="profile-signature" id="profileSignature"></div>
            <div class="profile-sig-input" id="profileSigInput" style="display:none;">
                <input type="text" id="profileSigText" placeholder="设置签名（最多200字）" maxlength="200">
                <button onclick="saveSignature()">保存</button>
            </div>
            <div class="profile-messages" id="profileMessages"></div>
        </div>
    </div>

</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, today=get_today())


# ========== 启动 ==========
if __name__ == '__main__':
    print("🚀 Agent 协作平台启动中...")
    print(f"📍 访问地址: http://localhost:{PORT}")
    print("📡 SSE 实时推送已启用: /api/stream")
    
    # 启动 SSE 广播器后台线程
    broadcaster.start_background_thread()
    
    try:
        # SSE 需要禁用 reloader 和 debug 以避免缓冲问题
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True, use_reloader=False)
    finally:
        # 关闭时清理
        broadcaster.stop()
        print("🛑 平台已关闭")
