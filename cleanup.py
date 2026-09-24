from django.db import connection
cursor = connection.cursor()
cursor.execute("""
    DELETE FROM authtoken_token
    WHERE user_id IN (SELECT id FROM users_user WHERE email != %s)
""", ["hello.hopenix@gmail.com"])
print("Tokens deleted:", cursor.rowcount)
cursor.execute("DELETE FROM users_user WHERE email != %s", ["hello.hopenix@gmail.com"])
print("Users deleted:", cursor.rowcount)
