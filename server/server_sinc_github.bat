robocopy C:\PROOFER\server C:\PROOFER\proofok\server /E /XF .env /XD uploads data __pycache__
cd C:\PROOFER\proofok
git add server
git commit -m "Respond fast: EMAIL_MODE + SMTP_TIMEOUT + version tag"
git pull --rebase origin main
git push

