#!/usr/bin/env python3
"""
测试脚本：验证 skip patterns 功能
"""

import tempfile
import os
from pathlib import Path
from fragmented_memory import FragmentedMemoryProvider

def test_skip_patterns():
    """测试 skip patterns 功能"""
    
    # 创建临时配置文件
    config_content = {
        "redis_host": "127.0.0.1",
        "redis_port": 6379,
        "agent_id": "test_agent",
        "skip_min_length": 2,
        "skip_patterns_file": "/tmp/skip_patterns.txt"
    }
    
    # 创建 skip patterns 文件
    with open("/tmp/skip_patterns.txt", "w") as f:
        f.write("# 测试跳过的模式\n")
        f.write("好\n")
        f.write("可以\n")
        f.write("嗯\n")
        f.write("ok\n")
        f.write("\n")  # 空行
        f.write("# 这是注释行\n")
    
    try:
        # 初始化 provider
        provider = FragmentedMemoryProvider(**config_content)
        
        # 测试应该跳过的短查询
        print("测试跳过的查询:")
        for query in ["好", "可以", "嗯", "ok"]:
            result = provider._should_search(query)
            print(f"  '{query}': {result}")
            
        # 测试应该进行检索的查询
        print("\n测试应该检索的查询:")
        for query in ["分析BTC", "Redis密码", "删了", "你好世界"]:
            result = provider._should_search(query)
            print(f"  '{query}': {result}")
            
        # 测试 prefetch 方法
        print("\n测试 prefetch 方法:")
        for query in ["好", "可以", "分析BTC", "Redis密码"]:
            result = provider.prefetch(query)
            print(f"  '{query}': 返回长度 = {len(result)}")
            
        print("\n测试完成!")
        
    finally:
        # 清理临时文件
        if os.path.exists("/tmp/skip_patterns.txt"):
            os.remove("/tmp/skip_patterns.txt")

if __name__ == "__main__":
    test_skip_patterns()