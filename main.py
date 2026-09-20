from scripts.generation import generate_response
from scripts.query_db import ask


def main():
    print("Chat started. Type 'quit' to exit.\n")
    
    while True:
        # Get user input
        user_query = str(input("You: ")).strip()
        db_context = ask(user_query)
        
        # Check for exit conditions
        if user_query.lower() in ['quit', 'exit', 'bye']:
            print("Bot: Goodbye!")
            break
        
        # Skip empty inputs
        if not user_query:
            continue
        
        response = generate_response(user_query, db_context)
        
        print("\n------------- Response from the model: -------------")
        print(f"Bot:\n{response}")



if __name__ == "__main__":
    main()
