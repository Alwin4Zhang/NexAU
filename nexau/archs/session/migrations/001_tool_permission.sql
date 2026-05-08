-- RFC-0019: 工具权限管理
-- 幂等：使用 IF NOT EXISTS，可重复执行

CREATE TABLE IF NOT EXISTS permission_rules (
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    rule_content TEXT NOT NULL,
    behavior    TEXT NOT NULL CHECK (behavior IN ('allow', 'deny')),
    source      TEXT NOT NULL CHECK (source IN ('config', 'user')),
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, session_id, tool_name, rule_content, behavior)
);

-- SQLite 不支持 ADD COLUMN IF NOT EXISTS，用 PRAGMA 检测后条件执行。
-- 框架层用 Python 检测列是否存在，不存在时执行：
-- ALTER TABLE sessions ADD COLUMN pending_tool_calls JSON DEFAULT NULL;
