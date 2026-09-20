import os
from openai import AzureOpenAI
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import pandas as pd

load_dotenv()

endpoint = os.getenv("ENDPOINT_URL")
model_name = os.getenv("MODEL_NAME")
deployment = os.getenv("DEPLOYMENT_NAME")
subscription_key = os.getenv("OPENAI_KEY")
api_version = "2024-12-01-preview"

client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=endpoint,
    api_key=subscription_key,
)

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)

SCHEMA_CONTEXT = """
Database schema (Chinook):
- Artist (ArtistId, Name)
- Album (AlbumId, Title, ArtistId)
- Track (TrackId, Name, AlbumId, MediaTypeId, GenreId, Composer, Milliseconds, Bytes, UnitPrice)
- Genre (GenreId, Name)
- MediaType (MediaTypeId, Name)
- Customer (CustomerId, FirstName, LastName, Company, Address, City, State, Country, PostalCode, Phone, Fax, Email, SupportRepId)
- Employee (EmployeeId, LastName, FirstName, Title, ReportsTo, BirthDate, HireDate, Address, City, State, Country, PostalCode, Phone, Fax, Email)
- Invoice (InvoiceId, CustomerId, InvoiceDate, BillingAddress, BillingCity, BillingState, BillingCountry, BillingPostalCode, Total)
- InvoiceLine (InvoiceLineId, InvoiceId, TrackId, UnitPrice, Quantity)
- Playlist (PlaylistId, Name)
- PlaylistTrack (PlaylistId, TrackId)
"""

def generate_sql(question: str) -> str:
    system_prompt = f"""You are an expert SQL analyst with deep understanding of both natural language semantics and database structures. Your task is to interpret the user's true intent and write a PostgreSQL query that fulfills it.

{SCHEMA_CONTEXT}

CRITICAL - Semantic Intent Mapping:
Users often use natural language that doesn't literally match schema names. You must map their intent to the correct table/column:

- "singer", "artist", "musician", "band", "performer", "act" → "Artist" table
- "song", "music", "track", "piece" → "Track" table
- "album", "record", "release", "LP" → "Album" table
- "buyer", "client", "purchaser", "user" → "Customer" table
- "staff", "worker", "personnel" → "Employee" table
- "bill", "receipt", "purchase", "order", "sale" → "Invoice" table
- "genre", "style", "category" → "Genre" table
- "playlist", "list", "collection" → "Playlist" table
- "format", "type", "medium" → "MediaType" table

KEY INSIGHT: When a user asks "how many X?", they almost always mean "count the rows in the entity that represents X", NOT "count rows in a table literally named X". For example:
- "how many singers?" → SELECT COUNT(*) FROM "Artist"  (NOT a table called "Singer")
- "how many songs?" → SELECT COUNT(*) FROM "Track"
- "how many albums?" → SELECT COUNT(*) FROM "Album"
- "how many customers?" → SELECT COUNT(*) FROM "Customer"

Before writing the query, silently reason about:
1. What entity is the user actually asking about?
2. Does that entity map directly to a table, or is it a semantic synonym?
3. Is the question about counting rows, aggregating values, filtering, or joining data?

Rules:
- Return ONLY the SQL query, no explanations or markdown fences
- Use proper PostgreSQL syntax
- Use double quotes for table/column names (e.g., "Artist"."Name")
- Limit results to 50 rows unless the query is an aggregate (COUNT, SUM, AVG, etc.)
- Use explicit column names, not SELECT *
- For "how many" questions, use COUNT(*) or COUNT("Column") as appropriate
- When ambiguous, prefer the interpretation that answers a meaningful business question
"""
    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        max_completion_tokens=2000,
        temperature=0,
        model=deployment,
    )
    sql = response.choices[0].message.content.strip()
    # Remove markdown code fences if present
    if sql.startswith("```"):
        sql = sql.split("\n", 1)[1]
        if sql.endswith("```"):
            sql = sql.rsplit("\n", 1)[0]
    return sql.strip()

def execute_query(sql: str) -> pd.DataFrame:
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        df = pd.DataFrame(result.fetchall(), columns=result.keys())
    return df

def ask(question: str):
    sql = generate_sql(question)
    try:
        df = execute_query(sql)
        print(f"Results ({len(df)} rows):")
        print(df.to_string(index=False))
    except Exception as e:
        print(f"Error executing query: {e}")

# if __name__ == "__main__":
#     questions = [
#             "How many singers?"
#         ]
#     for q in questions:
#         ask(q)
#         print("\n" + "="*60 + "\n")