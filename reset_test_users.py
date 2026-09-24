from django.db import connection

ADMIN_EMAIL = "hello.hopenix@gmail.com"

cursor = connection.cursor()
cursor.execute(
    "DELETE FROM authtoken_token WHERE user_id IN (SELECT id FROM users_user WHERE email != %s)",
    [ADMIN_EMAIL],
)
print("Tokens deleted:", cursor.rowcount)

cursor.execute("DELETE FROM users_user WHERE email != %s", [ADMIN_EMAIL])
print("Users deleted:", cursor.rowcount)
