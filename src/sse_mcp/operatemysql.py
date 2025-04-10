import os

import uvicorn
from mcp.server.sse import SseServerTransport
from mysql.connector import connect, Error
from mcp.server import Server
from mcp.types import  Tool, TextContent
from pypinyin import pinyin, Style
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from dotenv import load_dotenv


ALLOW_METHODS = (os.getenv("ALLOW_METHODS") or "select,update,show").split(',')


def get_db_config():

    """从环境变量获取数据库配置信息

    返回:
        dict: 包含数据库连接所需的配置信息
        - host: 数据库主机地址
        - port: 数据库端口
        - user: 数据库用户名
        - password: 数据库密码
        - database: 数据库名称

    异常:
        ValueError: 当必需的配置信息缺失时抛出
    """

    # 加载.env文件
    load_dotenv()

    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE")
    }
    if not all([config["user"], config["password"], config["database"]]):
        raise ValueError("缺少必需的数据库配置")

    return config


def get_chinese_initials(text) -> list[TextContent]:
    """将中文文本转换为拼音首字母

    参数:
        text (str): 要转换的中文文本，以中文逗号分隔

    返回:
        list[TextContent]: 包含转换结果的TextContent列表
        - 每个词的首字母会被转换为大写
        - 多个词的结果以英文逗号连接

    示例:
        >>> get_chinese_initials("用户名，密码")
        [TextContent(type="text", text="YHM,MM")]
    """
    # 将文本按逗号分割
    words = text.split('，')

    # 存储每个词的首字母
    initials = []

    for word in words:
        # 获取每个字的拼音首字母
        word_pinyin = pinyin(word, style=Style.FIRST_LETTER)
        # 将每个字的首字母连接起来
        word_initials = ''.join([p[0].upper() for p in word_pinyin])
        initials.append(word_initials)

    # 用逗号连接所有结果
    return [TextContent(type="text", text=','.join(initials))]


def my_check(statement: str) -> str:
    if ';' in statement:
        return "【内部安全检查】禁止一次执行多条"
    
    cmd = statement.split(' ')[0].lower()
    if cmd not in ALLOW_METHODS:
        return f"【内部安全检查】不允许的操作：{cmd}, 目前允许{ALLOW_METHODS}"
    return ""


def execute_sql(query : str) -> list[TextContent]:
    """执行SQL查询语句

    参数:
        query (str): 要执行的SQL语句，支持多条语句以分号分隔

    返回:
        list[TextContent]: 包含查询结果的TextContent列表
        - 对于SELECT查询：返回CSV格式的结果，包含列名和数据
        - 对于SHOW TABLES：返回数据库中的所有表名
        - 对于其他查询：返回执行状态和影响行数
        - 多条语句的结果以"---"分隔

    异常:
        Error: 当数据库连接或查询执行失败时抛出
    """
    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                statements = [stmt.strip() for stmt in query.split(';') if stmt.strip()]
                results = []

                for statement in statements:
                    try:
                        error_msg = my_check(statement)
                        if error_msg:
                            raise ValueError(error_msg)

                        cursor.execute(statement)

                        # 检查语句是否返回了结果集 (SELECT, SHOW, EXPLAIN, etc.)
                        if cursor.description:
                            columns = [desc[0] for desc in cursor.description]
                            rows = cursor.fetchall()
                            
                            # 将每一行的数据转换为字符串，特殊处理None值
                            formatted_rows = []
                            for row in rows:
                                formatted_row = ["NULL" if value is None else str(value) for value in row]
                                formatted_rows.append(",".join(formatted_row))
                                
                            # 将列名和数据合并为CSV格式
                            results.append("\n".join([",".join(columns)] + formatted_rows))
                        
                        # 如果语句没有返回结果集 (INSERT, UPDATE, DELETE, etc.)
                        else:
                            conn.commit() # 只有在非查询语句时才提交
                            results.append(f"查询执行成功。影响行数: {cursor.rowcount}")

                    except Error as stmt_error:
                        # 单条语句执行出错时，记录错误并继续执行
                        results.append(f"执行语句 '{statement}' 出错: {str(stmt_error)}")
                        # 可以在这里选择是否继续执行后续语句，目前是继续
                        
                return [TextContent(type="text", text="\n---\n".join(results))]

    except Error as e:
        print(f"执行SQL '{query}' 时出错: {e}")
        return [TextContent(type="text", text=f"执行查询时出错: {str(e)}")]

def get_table_name(text : str) -> list[TextContent]:
    """根据表的中文注释搜索数据库中的表名

    参数:
        text (str): 要搜索的表中文注释关键词

    返回:
        list[TextContent]: 包含查询结果的TextContent列表
        - 返回匹配的表名、数据库名和表注释信息
        - 结果以CSV格式返回，包含列名和数据
    """
    config = get_db_config()
    sql = "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_COMMENT "
    sql += f"FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{config['database']}' AND TABLE_COMMENT LIKE '%{text}%';"
    return execute_sql(sql)

def get_table_desc(text : str) -> list[TextContent]:
    """获取指定表的字段结构信息

    参数:
        text (str): 要查询的表名，多个表名以逗号分隔

    返回:
        list[TextContent]: 包含查询结果的TextContent列表
        - 返回表的字段名、字段注释等信息
        - 结果按表名和字段顺序排序
        - 结果以CSV格式返回，包含列名和数据
    """
    config = get_db_config()
    # 将输入的表名按逗号分割成列表
    table_names = [name.strip() for name in text.split(',')]
    # 构建IN条件
    table_condition = "','".join(table_names)
    sql = "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT "
    sql += f"FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = '{config['database']}' "
    sql += f"AND TABLE_NAME IN ('{table_condition}') ORDER BY TABLE_NAME, ORDINAL_POSITION;"
    return execute_sql(sql)

def get_table_index(text : str) -> list[TextContent]:
    """获取指定表的索引信息

    参数:
        text (str): 要查询的表名，多个表名以逗号分隔

    返回:
        list[TextContent]: 包含查询结果的TextContent列表
        - 返回表的索引名、索引字段、索引类型等信息
        - 结果按表名、索引名和索引顺序排序
        - 结果以CSV格式返回，包含列名和数据
    """
    config = get_db_config()
    # 将输入的表名按逗号分割成列表
    table_names = [name.strip() for name in text.split(',')]
    # 构建IN条件
    table_condition = "','".join(table_names)
    sql = "SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX, NON_UNIQUE, INDEX_TYPE "
    sql += f"FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = '{config['database']}' "
    sql += f"AND TABLE_NAME IN ('{table_condition}') ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;"
    return execute_sql(sql)


def get_lock_tables() -> list[TextContent]:
    sql = "SELECT p2.`HOST` 被阻塞方host,  p2.`USER` 被阻塞方用户, r.trx_id 被阻塞方事务id, "
    sql += "r.trx_mysql_thread_id 被阻塞方线程号,TIMESTAMPDIFF(SECOND, r.trx_wait_started, CURRENT_TIMESTAMP) 等待时间, "
    sql += "r.trx_query 被阻塞的查询, l.lock_table 阻塞方锁住的表, m.`lock_mode` 被阻塞方的锁模式, "
    sql += "m.`lock_type` '被阻塞方的锁类型(表锁还是行锁)', m.`lock_index` 被阻塞方锁住的索引, "
    sql += "m.`lock_space` 被阻塞方锁对象的space_id, m.lock_page 被阻塞方事务锁定页的数量, "
    sql += "m.lock_rec 被阻塞方事务锁定记录的数量, m.lock_data 被阻塞方事务锁定记录的主键值, "
    sql += "p.`HOST` 阻塞方主机, p.`USER` 阻塞方用户, b.trx_id 阻塞方事务id,b.trx_mysql_thread_id 阻塞方线程号, "
    sql += "b.trx_query 阻塞方查询, l.`lock_mode` 阻塞方的锁模式, l.`lock_type` '阻塞方的锁类型(表锁还是行锁)',"
    sql += "l.`lock_index` 阻塞方锁住的索引,l.`lock_space` 阻塞方锁对象的space_id,l.lock_page 阻塞方事务锁定页的数量,"
    sql += "l.lock_rec 阻塞方事务锁定行的数量,  l.lock_data 阻塞方事务锁定记录的主键值,"
    sql += "IF(p.COMMAND = 'Sleep', CONCAT(p.TIME, ' 秒'), 0) 阻塞方事务空闲的时间 "
    sql += "FROM information_schema.INNODB_LOCK_WAITS w "
    sql += "INNER JOIN information_schema.INNODB_TRX b ON b.trx_id = w.blocking_trx_id "
    sql += "INNER JOIN information_schema.INNODB_TRX r ON r.trx_id = w.requesting_trx_id "
    sql += "INNER JOIN information_schema.INNODB_LOCKS l ON w.blocking_lock_id = l.lock_id AND l.`lock_trx_id` = b.`trx_id` "
    sql += "INNER JOIN information_schema.INNODB_LOCKS m ON m.`lock_id` = w.`requested_lock_id` AND m.`lock_trx_id` = r.`trx_id` "
    sql += "INNER JOIN information_schema.PROCESSLIST p ON p.ID = b.trx_mysql_thread_id "
    sql += "INNER JOIN information_schema.PROCESSLIST p2 ON p2.ID = r.trx_mysql_thread_id "
    sql += "ORDER BY 等待时间 DESC;"

    return execute_sql(sql)

# 初始化服务器
app = Server("operateMysql")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """列出可用的MySQL工具

    返回:
        list[Tool]: 工具列表，当前仅包含execute_sql工具
    """
    return [
        Tool(
            name="execute_sql",
            description="在MySQL5.6s数据库上执行SQL",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要执行的SQL语句"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="get_chinese_initials",
            description="创建表结构时，将中文字段名转换为拼音首字母字段",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要获取拼音首字母的汉字文本，以“,”分隔"
                    }
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="get_table_name",
            description="根据表中文名搜索数据库中对应的表名",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要搜索的表中文名"
                    }
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="get_table_desc",
            description="根据表名搜索数据库中对应的表结构,支持多表查询",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要搜索的表名"
                    }
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="get_table_index",
            description="根据表名搜索数据库中对应的表索引,支持多表查询",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要搜索的表名"
                    }
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="get_lock_tables",
            description="获取当前mysql服务器InnoDB 的行级锁",
            inputSchema={
                "type": "object",
                "properties": {

                }
            }
        ),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:

    if name == "execute_sql":
        query = arguments.get("query")
        if not query:
            raise ValueError("缺少查询语句")
        return execute_sql(query)
    elif name == "get_chinese_initials":
        text = arguments.get("text")
        if not text:
            raise ValueError("缺少文本")
        return get_chinese_initials(text)
    elif name == "get_table_name":
        text = arguments.get("text")
        if not text:
            raise ValueError("缺少表信息")
        return get_table_name(text)
    elif name == "get_table_desc":
        text = arguments.get("text")
        if not text:
            raise ValueError("缺少表信息")
        return get_table_desc(text)
    elif name == "get_table_index":
        text = arguments.get("text")
        if not text:
            raise ValueError("缺少表信息")
        return get_table_index(text)
    elif name == "get_lock_tables":
        return get_lock_tables()

    raise ValueError(f"未知的工具: {name}")

sse = SseServerTransport("/messages/")

# Handler for SSE connections
async def handle_sse(request):
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await app.run(streams[0], streams[1], app.create_initialization_options())


# Create Starlette app with routes
starlette_app = Starlette(
    debug=True,
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/",  app=sse.handle_post_message)
    ],
)


if __name__ == "__main__":
    uvicorn.run(starlette_app, host="0.0.0.0", port=9003)
