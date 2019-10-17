# SocialMediaSchedulerApi

This folder has 1 Python file which is social_apis.py (primary file that has most of the code required to do social media api calls). This file is **OWNED BY DRUMUP** and is **not authorized** for use by anyone other than Drumup and the owner of the file(Santhosh Raj).

![Image of social media scheduler](https://photos.app.goo.gl/CLXpKPpc6wCoPk4R8)

The file has Python functions that does the following
1. Retrieves the access token of the users with permissions for posting on behalf of users on their timeline
2. Pushes the articles scheduled by the users on their respective social media timelines
3. Refreshes the access tokens of users periodically
4. Makes Aws calls to retrieve any video or images associated with the article the user wants to share on their timeline
5. Get details about all the pages and groups owned by the user on Facebook and Linkedin
6. Fetches user's timeline, informations about a user's social media connections and other personal information the user authorizes the app for and performs analytics on the same

Primary Libraries:
1. twython
2. facebook-sdk
3. linkedin (marketing api)
4. InstagramApi
5. boto
