def is_banned(user_id):
    try:
        with open('banned_users.txt', 'r') as f:
            banned = [int(line.strip()) for line in f.readlines() if line.strip()]
        return user_id in banned
    except FileNotFoundError:
        return False
    except ValueError:
        return False


def ban_user_id(user_id):
    if is_banned(user_id):
        return False

    with open('banned_users.txt', 'a') as f:
        f.write(f"{user_id}\n")
    return True


def unban_user_id(user_id):
    try:
        with open('banned_users.txt', 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return False

    found = False
    with open('banned_users.txt', 'w') as f:
        for line in lines:
            if line.strip() != str(user_id):
                f.write(line)
            else:
                found = True

    return found
