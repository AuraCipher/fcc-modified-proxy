"""Test token tracking persistence across server restarts."""

import tempfile
from pathlib import Path

# Temporarily override the database path for testing
from unittest.mock import patch


def test_token_persistence():
    """Demonstrate that token data persists across tracker reloads."""

    # Create a temporary database for testing
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "token_tracking_test.db"

        # Mock the database path
        def mock_get_db_path():
            return db_path

        with patch("core.token_tracking._get_db_path", mock_get_db_path):
            # Reset singleton for clean test
            from core.token_tracking import TokenTracker

            TokenTracker.reset_instance()

            # ===== PHASE 1: Add tokens =====
            print("=" * 60)
            print("PHASE 1: Add tokens and save to database")
            print("=" * 60)

            tracker1 = TokenTracker.get_instance()

            # Add some token usage
            tracker1.add_tokens(
                "nvidia_nim", "z-ai/glm4.7", input_tokens=150, output_tokens=250
            )
            tracker1.add_tokens(
                "nvidia_nim", "deepseek-v4-pro", input_tokens=200, output_tokens=300
            )
            tracker1.add_tokens(
                "openrouter", "gpt-4-turbo", input_tokens=100, output_tokens=150
            )

            # Get current state
            report1 = tracker1.get_report()
            print("\n✓ Tokens recorded:")
            print(f"  Total input: {report1['total']['input_tokens']}")
            print(f"  Total output: {report1['total']['output_tokens']}")
            print(f"  Total requests: {report1['total']['request_count']}")
            print(f"  Database location: {db_path}")
            print(f"  Database exists: {db_path.exists()}")
            if db_path.exists():
                print(f"  Database size: {db_path.stat().st_size} bytes")

            # ===== PHASE 2: Simulate server restart =====
            print("\n" + "=" * 60)
            print("PHASE 2: Simulate server restart (reset tracker)")
            print("=" * 60)

            TokenTracker.reset_instance()
            print("✓ Tracker reset (simulating server shutdown)")

            # ===== PHASE 3: Reload and verify persistence =====
            print("\n" + "=" * 60)
            print("PHASE 3: Reload tracker (simulating server startup)")
            print("=" * 60)

            tracker2 = TokenTracker.get_instance()
            report2 = tracker2.get_report()

            print("\n✓ Tokens restored from database:")
            print(f"  Total input: {report2['total']['input_tokens']}")
            print(f"  Total output: {report2['total']['output_tokens']}")
            print(f"  Total requests: {report2['total']['request_count']}")

            # ===== PHASE 4: Verify complete data integrity =====
            print("\n" + "=" * 60)
            print("PHASE 4: Verify data integrity")
            print("=" * 60)

            assert report2["total"]["input_tokens"] == 450, "Input tokens mismatch"
            assert report2["total"]["output_tokens"] == 700, "Output tokens mismatch"
            assert report2["total"]["request_count"] == 3, "Request count mismatch"

            # Check provider breakdown
            assert "nvidia_nim" in report2["by_provider"], "nvidia_nim provider missing"
            assert "openrouter" in report2["by_provider"], "openrouter provider missing"

            # Check model breakdown
            assert "z-ai/glm4.7" in report2["by_provider"]["nvidia_nim"]["by_model"]
            assert "gpt-4-turbo" in report2["by_provider"]["openrouter"]["by_model"]

            print("✓ All data integrity checks passed!")
            print(f"  - Providers found: {list(report2['by_provider'].keys())}")
            print(
                f"  - Models found: {sum(len(m['by_model']) for m in report2['by_provider'].values())}"
            )

            # ===== PHASE 5: Add more tokens and verify accumulation =====
            print("\n" + "=" * 60)
            print("PHASE 5: Add more tokens after restart")
            print("=" * 60)

            tracker2.add_tokens(
                "nvidia_nim", "z-ai/glm4.7", input_tokens=100, output_tokens=200
            )
            report3 = tracker2.get_report()

            print("\n✓ Additional tokens recorded:")
            print(f"  Total input: {report3['total']['input_tokens']}")
            print(f"  Total output: {report3['total']['output_tokens']}")
            print(f"  Total requests: {report3['total']['request_count']}")

            assert report3["total"]["input_tokens"] == 550, (
                "Input tokens after accumulation mismatch"
            )
            assert report3["total"]["output_tokens"] == 900, (
                "Output tokens after accumulation mismatch"
            )
            assert report3["total"]["request_count"] == 4, (
                "Request count after accumulation mismatch"
            )

            print("✓ Token accumulation works correctly across restarts!")

            # ===== PHASE 6: Test cleanup old data =====
            print("\n" + "=" * 60)
            print("PHASE 6: Test data cleanup")
            print("=" * 60)

            # Note: In real usage, cleanup happens on startup for data older than 30 days
            # We can't properly test this in unit tests without mocking time
            deleted = tracker2.cleanup_old_data(days=30)
            print(f"✓ Cleanup executed (deleted {deleted} old records)")

            print("\n" + "=" * 60)
            print("✅ PERSISTENCE TEST PASSED")
            print("=" * 60)
            print("\nSummary:")
            print("  ✓ Tokens persist to SQLite database")
            print("  ✓ Data survives tracker resets (simulated restarts)")
            print("  ✓ Complete hierarchical structure maintained")
            print("  ✓ Token accumulation works across restarts")
            print("  ✓ Cleanup functionality available for old data")


if __name__ == "__main__":
    test_token_persistence()
