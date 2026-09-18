"""应用配置（pydantic-settings）。

对应主方案《01_蜜罐反诈V2_双形态技术方案与实施计划.md》§6.3（LLM 路由与预算）、
§5.4（知乎合规/频率控制）。所有配置项以 AF_ 为前缀，从环境变量或 .env 文件读取。

示例：AF_PORT=9200 -> settings.port
模板见项目根 .env.example；测试中可用 monkeypatch.setenv + get_settings.cache_clear()
动态切换配置。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from . import __version__


class Settings(BaseSettings):
    """全局配置单例。字段名去掉 AF_ 前缀后与 env 对齐。"""

    # ---- 主控 ----
    host: str = "127.0.0.1"
    port: int = 9200
    data_dir: str = "data"          # 相对项目根解析；绝对路径直接使用

    # ---- 认证 ----
    # AF_API_KEY 非空时作为 bootstrap admin key 的显式覆盖（优先级高于 data/ 文件）；
    # 空则由主控首次启动自动生成 data/bootstrap_admin_key.txt（格式 af_admin_xxxxxxxx）。
    api_key: str = ""

    # ---- LLM（OpenAI 兼容；DeepSeek 默认或本地 Ollama/vLLM）----
    llm_base_url: str = ""          # 留空 => LLM 未配置（health 显示 not_configured，D5 降级）
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = 30.0
    llm_daily_budget: int = 0       # 0=不限额；>0=每日次数上限（预算护栏，P3 落地）

    # ---- 知乎通道 ----
    zhihu_rate_limit: int = 20      # 全局令牌桶：请求/分钟
    zhihu_encrypt_key: str = ""     # 留空则首次启动自动生成 data/secret.key（P2 落地）
    zhihu_key_file: str = ""        # P3-8：Fernet 密钥独立文件路径（优先于 data/secret.key，相对路径基于项目根）
    zhihu_rsshub_base: str = "https://rsshub.app"  # CH-C RSSHub 只读公开聚合入口；留空=未配置
    zhihu_ch_a_headless: bool = True  # CH-A Playwright headless（常驻浏览器减负，§5.2）

    # ---- 全局请求级限流（P2-5）----
    rate_limit_per_min: int = 60         # 公开热路径（scan/text、accounts/check 等）按 IP 令牌桶配额；0=关闭
    rate_limit_admin_bypass: bool = True  # 携带有效 admin key 的请求豁免限流

    model_config = SettingsConfigDict(
        env_prefix="AF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def version(self) -> str:
        return __version__

    @property
    def llm_configured(self) -> bool:
        """LLM 是否完成配置（base_url / api_key / model 三者齐备才算）。
        未配置时系统必须能正常启动并降级运行（D5）。"""
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


@lru_cache
def get_settings() -> Settings:
    """懒加载配置单例。测试中修改环境变量后调用 get_settings.cache_clear() 刷新。"""
    return Settings()
