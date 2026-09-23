#!/usr/bin/env python3
"""
Main entry point for the Chinook Database Chat Application.

This module provides a command-line interface for interacting with the
Chinook database using natural language queries powered by Azure OpenAI.

Environment Variables Required:
    AZURE_OPENAI_KEY: Azure OpenAI API key
    AZURE_OPENAI_ENDPOINT: Azure OpenAI endpoint URL
    DEPLOYMENT_NAME: Azure OpenAI deployment name
    MODEL_NAME: Model name for generation
    DATABASE_URL: PostgreSQL connection string
    DATABASE_PASSWORD: Database password (if not in DATABASE_URL)
"""

import os
import sys
from dotenv import load_dotenv
from scripts.generator import ask, generate_response
from scripts.db_module import index_schema, conn


def check_environment():
    """Verify all required environment variables are set."""
    required_vars = [
        "AZURE_OPENAI_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "DEPLOYMENT_NAME",
        "MODEL_NAME",
        "DATABASE_URL",
    ]
    
    missing = [var for var in required_vars if not os.environ.get(var)]
    
    if missing:
        print("Error: Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        print("\nPlease set these in your .env file or environment.")
        return False
    return True


def print_welcome():
    """Print welcome message and usage instructions."""
    print("=" * 60)
    print("  Chinook Database Chat - Natural Language Query Interface")
    print("=" * 60)
    print("\nCommands:")
    print("  - Type your question in natural language")
    print("  - 'index'  : Re-index the database schema")
    print("  - 'help'   : Show this help message")
    print("  - 'quit'   : Exit the application")
    print("-" * 60)


def print_help():
    """Print detailed help message."""
    print("\nUsage Examples:")
    print("  - How many customers are from the USA?")
    print("  - What are the top 5 selling tracks?")
    print("  - Show me all albums by AC/DC")
    print("  - Total sales by country in 2023")
    print("\nSpecial Commands:")
    print("  - index  : Rebuild the vector index for schema search")
    print("  - help   : Show this help")
    print("  - quit   : Exit")


def main():
    """Main application loop."""
    load_dotenv()
    
    if not check_environment():
        sys.exit(1)
    
    print_welcome()
    
    # Check if index needs to be built
    from scripts.db_module import qdrant, COLLECTION
    if not qdrant.collection_exists(COLLECTION):
        print("\nSchema index not found. Building index...")
        index_schema(conn)
        print("Index built successfully!\n")
    
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
            
            if cmd == 'index':
                print("\nRe-indexing schema...")
                index_schema(conn)
                print("Done!")
                continue
            
            # Process natural language query
            print("\nProcessing...")
            response = ask(user_query)
            
            print("\n" + "=" * 60)
            print("Response:")
            print("=" * 60)
            print(response)
            print("=" * 60)
            
        except KeyboardInterrupt:
            print("\n\nInterrupted. Goodbye!")
            break
        except EOFError:
            print("\n\nGoodbye!")
            break
        except Exception as e:
            print(f"\nError: {e}")
            print("Please try again or type 'help' for assistance.")


if __name__ == "__main__":
    main()