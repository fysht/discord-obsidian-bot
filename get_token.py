import dropbox

APP_KEY = input("Enter your Dropbox App Key: ").strip()
APP_SECRET = input("Enter your Dropbox App Secret: ").strip()

auth_flow = dropbox.DropboxOAuth2FlowNoRedirect(
    APP_KEY,
    APP_SECRET,
    token_access_type='offline'
)

authorize_url = auth_flow.start()
print("1. Go to: " + authorize_url)
print("2. Click 'Allow' (you might have to log in first).")
print("3. Copy the authorization code.")
auth_code = input("Enter the authorization code here: ").strip()

try:
    oauth_result = auth_flow.finish(auth_code)
    print("\nSUCCESS! Your refresh token is:\n")
    print(oauth_result.refresh_token)
    print("\nCopy this token and set it as the DROPBOX_REFRESH_TOKEN environment variable on Render.")
except Exception as e:
    print('Error: %s' % (e,))