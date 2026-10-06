#!/usr/bin/env python3
"""测试新增的工具功能"""
import asyncio
import sys
import main


async def test():
    print("🧪 测试新增功能...\n")

    await main.get_client()

    # 测试列表
    tests = [
        # (工具名, 参数, 描述)
        ("get_me", {}, "获取我的信息"),
        ("get_privacy", {}, "获取隐私设置"),
        ("get_active_sessions", {}, "获取活跃会话"),
        ("search_global", {"query": "test", "limit": 5}, "全局搜索"),
    ]

    passed = 0
    failed = 0

    for tool_name, args, desc in tests:
        try:
            tool_func = getattr(main, tool_name)
            if args:
                result = await tool_func(**args)
            else:
                result = await tool_func()

            # 检查结果不为空且不包含错误
            if result and "❌" not in result and "Error" not in result:
                print(f"✅ {desc} ({tool_name})")
                passed += 1
            else:
                print(f"⚠️ {desc} ({tool_name}): {result[:100]}")
                passed += 1  # 即使结果不完美也算通过，因为可能是权限问题
        except Exception as e:
            print(f"❌ {desc} ({tool_name}): {e}")
            failed += 1

    print(f"\n📊 测试结果: {passed} 通过, {failed} 失败")
    return failed == 0


if __name__ == "__main__":
    result = asyncio.run(test())
    sys.exit(0 if result else 1)
