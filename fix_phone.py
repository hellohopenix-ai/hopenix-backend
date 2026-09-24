from django.db import connection
c = connection.cursor()
c.execute("UPDATE users_profile SET phone = %s WHERE phone = %s", ["", "0327-5322568"])
print("Rows updated:", c.rowcount)
