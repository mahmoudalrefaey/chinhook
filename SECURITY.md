# Security policy

Chinhook connects to databases and model endpoints that strangers type in, so a security
problem here can affect people who never ran the code themselves. Reports are very welcome.

## Reporting a vulnerability

Please report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue for it.

Include what you found, how to reproduce it, and what an attacker could do with it. You will
get a reply as soon as it has been looked at, and credit in the fix if you want it.

## What is in scope

Anything that breaks one of the promises the app makes, for example:

- a query that writes to, or changes settings on, a connected database
- one visitor reaching another visitor's connection, chat, schema or index
- the server being made to connect to a private or internal address (see
  `connection.check_host_allowed`)
- a database password or model API key being stored, logged or shown anywhere
- script injection through a model's reply or a database value

## What is not

- The data a visitor chooses to send to their own model provider: the app tells them what it
  sends (see "What happens to your data" in README.md).
- Denial of service against a deployment by using it heavily within its configured rate
  limits; those limits are the operator's to set.
