"""Test token tracking functionality.

This demonstrates how token tracking works in the proxy.
"""

from core.token_tracking import get_token_tracker


def test_token_tracking() -> None:
    """Test token tracking with multiple providers and models."""
    tracker = get_token_tracker()
    tracker.clear()  # Start fresh

    # Simulate requests from different providers
    print("Testing Token Tracking System\n")
    print("=" * 60)

    # NVIDIA NIM requests
    print("\n1. Recording NVIDIA NIM tokens...")
    tracker.add_tokens("nvidia_nim", "z-ai/glm4.7", input_tokens=150, output_tokens=250)
    tracker.add_tokens("nvidia_nim", "z-ai/glm4.7", input_tokens=200, output_tokens=180)
    tracker.add_tokens("nvidia_nim", "deepseek-ai/deepseek-v4-pro", input_tokens=100, output_tokens=320)
    print("   ✓ Added 3 NIM requests")

    # OpenRouter requests
    print("\n2. Recording OpenRouter tokens...")
    tracker.add_tokens("openrouter", "openai/gpt-4.5-turbo", input_tokens=180, output_tokens=290)
    tracker.add_tokens("openrouter", "openai/gpt-4", input_tokens=160, output_tokens=210)
    print("   ✓ Added 2 OpenRouter requests")

    # DeepSeek requests
    print("\n3. Recording DeepSeek tokens...")
    tracker.add_tokens("deepseek", "deepseek-v4-pro", input_tokens=140, output_tokens=340)
    print("   ✓ Added 1 DeepSeek request")

    # Get full report
    print("\n" + "=" * 60)
    print("TOKEN USAGE REPORT\n")

    report = tracker.get_report()

    # Global totals
    total = report["total"]
    print(f"GLOBAL TOTALS:")
    print(f"  Input Tokens:  {total['input_tokens']:,}")
    print(f"  Output Tokens: {total['output_tokens']:,}")
    print(f"  Total Tokens:  {total['total_tokens']:,}")
    print(f"  Requests:      {total['request_count']}")
    print(f"  Avg/Request:   {total['avg_tokens_per_request']:,}")

    # By provider
    print(f"\nBY PROVIDER:")
    for provider_id, provider_data in report["by_provider"].items():
        prov_total = provider_data["total"]
        print(f"\n  {provider_id.upper()}")
        print(f"    Input:  {prov_total['input_tokens']:,}")
        print(f"    Output: {prov_total['output_tokens']:,}")
        print(f"    Total:  {prov_total['total_tokens']:,}")
        print(f"    Requests: {prov_total['request_count']}")

        # Models within provider
        if provider_data["by_model"]:
            print(f"    Models:")
            for model_id, model_stats in provider_data["by_model"].items():
                print(
                    f"      {model_id}: "
                    f"I={model_stats['input_tokens']}, "
                    f"O={model_stats['output_tokens']}, "
                    f"R={model_stats['request_count']}"
                )

    print("\n" + "=" * 60)
    print("\nAPI ENDPOINTS AVAILABLE:")
    print("  GET  /admin/api/tokens              - Full report")
    print("  GET  /admin/api/tokens/total        - Global totals")
    print("  GET  /admin/api/tokens/provider/{id} - Provider totals")
    print("  GET  /admin/api/tokens/provider/{id}/models - Models in provider")
    print("  GET  /admin/api/tokens/hierarchy    - Full provider/model hierarchy")
    print("  GET  /admin/api/tokens/model/{model} - Model across providers")
    print("  GET  /admin/api/tokens/history      - Hourly trending")
    print("  POST /admin/api/tokens/reset        - Reset all data")

    print("\n✓ Token tracking test completed successfully!\n")


if __name__ == "__main__":
    test_token_tracking()
