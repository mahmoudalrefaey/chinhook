import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.session import new_chat
from scripts.generator import ask_detailed, get_available_models
from scripts.indexer import run_index_check, get_index_status
import config


def print_welcome():
    """Print welcome message and usage instructions."""
    print("=" * 60)
    print("  Chinhook Database Chat - Natural Language Query Interface")
    print("=" * 60)
    print("\nCommands:")
    print("  - Type your question in natural language")
    print("  - 'index'     : Re-index the database schema")
    print("  - 'status'    : Check index status")
    print("  - 'model'     : Show/switch model (nano/mini)")
    print("  - 'internal'  : Toggle internal process preview")
    print("  - 'new'       : Start a new chat, forgetting the current one")
    print("  - 'help'      : Show this help message")
    print("  - 'quit'      : Exit the application")
    print("-" * 60)
    print(f"Available models: {', '.join(get_available_models())}")
    print(f"Current model: {config.DEFAULT_MODEL}")


def print_help():
    """Print detailed help message."""
    print("\nUsage Examples:")
    print("  - How many customers are from the USA?")
    print("  - What are the top 5 selling tracks?")
    print("  - Show me all albums by AC/DC")
    print("  - Total sales by country in 2023")
    print("\nSpecial Commands:")
    print("  - index     : Rebuild the vector index for schema search")
    print("  - status    : Show index status (DB vs Qdrant)")
    print("  - model     : Show current model or 'model nano|mini' to switch")
    print("  - internal  : Toggle internal process preview (on/off)")
    print("  - new       : Start a new chat (forgets the current conversation)")
    print("  - help      : Show this help")
    print("  - quit      : Exit")


def check_environment():
    """Verify all required environment variables are set."""
    valid, missing = config.validate_config()
    if not valid:
        print("Error: Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        print("\nPlease set these in your .env file or environment.")
        return False
    return True


def print_internal(result):
    """Show what happened inside the workflow for this question.

    Reads the run that just happened rather than repeating work to display it. The schema
    shown is the schema that task actually used, the tasks are the ones that were produced,
    and the token counts are the ones the run already recorded.
    """
    print("\n" + "=" * 60)
    print("Internal Process Preview:")
    print("=" * 60)
    print(f"Model Used: {result.get('model')}")
    if result.get("route"):
        print(f"Route: {result['route']}")

    for task in result.get("tasks") or []:
        state = task.get("status")
        tables = ", ".join(task.get("schema_tables") or []) or "none"
        reuse = " (reused from this chat)" if task.get("schema_from_cache") else ""
        print(f"\n  {task.get('task_id')} [{state}, verification: {task.get('verification')}] "
              f"attempt {task.get('attempts', 0) + 1}: {task.get('question')}")
        print(f"    Tables: {tables}{reuse}")
        if task.get("semantic"):
            print(f"    Grounding: {task['semantic']}")
        if task.get("sql"):
            print(f"    SQL: {task['sql']}")
        print(f"    Rows returned: {task.get('row_count', 0)}")
        if task.get("failure_kind"):
            print(f"    Failed because: {task.get('failure_kind')}: {task['failure_reason']}")

    usage = result.get("usage") or {}
    print("\n" + "-" * 60)
    print(
        f"Tokens: {usage.get('input_tokens', 0)} in, {usage.get('output_tokens', 0)} out, "
        f"{usage.get('total_tokens', 0)} total across {usage.get('llm_calls', 0)} model call(s)"
    )
    print("=" * 60)


def run_cli():
    """Run the CLI interface."""
    if not check_environment():
        sys.exit(1)

    print_welcome()

    # Auto-index on startup
    if config.AUTO_INDEX_ON_STARTUP:
        print("\nChecking index status...")
        try:
            count = run_index_check()
            print(f"Index ready ({count} tables)\n")
        except Exception as e:
            print(f"Index check failed: {e}\n")

    current_model = config.DEFAULT_MODEL
    show_internal = True

    # The conversation lives here and only here. Starting a new chat replaces this object
    # with an empty one, which is the only way context leaves the program.
    chat = new_chat()

    while True:
        try:
            user_query = input("\nYou: ").strip()

            if not user_query:
                continue

            # Handle special commands
            cmd = user_query.lower()
            if cmd in ['quit', 'exit', 'bye', 'q']:
                print("\nGoodbye!")
                break

            if cmd == 'help':
                print_help()
                continue

            if cmd == 'new':
                chat = new_chat()
                print("New chat. The previous conversation is forgotten.")
                continue

            if cmd == 'index':
                print("\nRe-indexing schema...")
                try:
                    from scripts.indexer import run_full_reindex
                    count = run_full_reindex()
                    print(f"Done! ({count} tables indexed)")
                except Exception as e:
                    print(f"Error: {e}")
                continue

            if cmd == 'status':
                try:
                    status = get_index_status()
                    print(f"\nDB Tables: {status['db_tables']}")
                    print(f"Qdrant Tables: {status['qdrant_tables']}")
                    print(f"Needs Re-index: {'Yes' if status['needs_reindex'] else 'No'}")
                    if status['changed_tables']:
                        print(f"Changed Tables: {', '.join(status['changed_tables'])}")
                except Exception as e:
                    print(f"Error: {e}")
                continue

            if cmd == 'model':
                print(f"\nCurrent model: {current_model}")
                print(f"Available: {', '.join(get_available_models())}")
                continue

            if cmd.startswith('model '):
                parts = cmd.split()
                if len(parts) > 1:
                    model = parts[1]
                    if model in get_available_models():
                        current_model = model
                        print(f"Model switched to: {current_model}")
                    else:
                        print(f"Unknown model: {model}")
                continue

            if cmd == 'internal':
                show_internal = not show_internal
                print(f"Internal process preview: {'ON' if show_internal else 'OFF'}")
                continue

            if cmd.startswith('internal '):
                parts = cmd.split()
                if len(parts) > 1:
                    val = parts[1].lower()
                    if val in ('on', 'true', 'yes', '1'):
                        show_internal = True
                        print("Internal process preview: ON")
                    elif val in ('off', 'false', 'no', '0'):
                        show_internal = False
                        print("Internal process preview: OFF")
                    else:
                        print("Usage: internal on|off")
                continue

            # Process natural language query
            print("\nProcessing...")
            try:
                result = ask_detailed(user_query, current_model, session=chat)

                if show_internal:
                    print_internal(result)

                print("\n" + "=" * 60)
                print("Response:")
                print("=" * 60)
                print(result.get("answer") or f"Error: {result.get('error')}")
                print("=" * 60)

            except Exception as e:
                print(f"\nError: {e}")
                print("Please try again or type 'help' for assistance.")

        except KeyboardInterrupt:
            print("\n\nInterrupted. Goodbye!")
            break
        except EOFError:
            print("\n\nGoodbye!")
            break


def main():
    """Main entry point - CLI only."""
    run_cli()

if __name__ == "__main__":
    main()
