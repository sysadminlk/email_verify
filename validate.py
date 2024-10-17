import validate_email
import smtplib
import dns.resolver

valid_emails = []
invalid_emails = []

def is_valid_email(email):
    # Validate email syntax
    is_valid = validate_email.validate_email(email)

    if not is_valid:
        return False

    # Validate email server and mailbox existence
    try:
        domain = email.split('@')[1]
        mx_records = dns.resolver.resolve(domain, 'MX')
        mx_list = [str(mx.exchange)[:-1] for mx in mx_records]
        for mx in mx_list:
            server = smtplib.SMTP(timeout=10)
            server.connect(mx)
            server.helo(server.local_hostname)
            server.mail('example@' + domain)
            code, message = server.rcpt(str(email))
            server.quit()
            if code == 250:
                return True
    except Exception:
        pass
    return False

with open('emails.txt', 'r', encoding='utf-8') as f:
    for line in f:
        email = line.strip()
        is_valid = is_valid_email(email)
        if is_valid:
            valid_emails.append(email)
            with open('valid.txt', 'a') as v:
                v.write(email + '\n')
                v.flush()
        else:
            invalid_emails.append(email)
            with open('invalid.txt', 'a') as iv:
                iv.write(email + '\n')
                iv.flush()

