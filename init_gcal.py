from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

def main():
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)

    # This starts a local server but DOES NOT auto-open the browser.
    # It will print a URL in the terminal for you to copy.
    creds = flow.run_local_server(port=0, open_browser=False)

    with open("token.json", "w") as f:
        f.write(creds.to_json())
    print("token.json created.")

if __name__ == "__main__":
    main()
