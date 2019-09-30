from twython import Twython
import facebook
import logging
from twython import TwythonAuthError
from django.utils import timezone
from datetime import datetime,timedelta
from models import FailedPosts, PostingAccounts, UAuthExtended, Buffer, UserSettings, FollowCounts, EAUsers, \
                   EAShares, EmailPreferences, BufferUserFeed, CustomPosts, EAPostShares, PostSociaMediaId, AutoAddTextAccount
from social.apps.django_app.default.models import UserSocialAuth
from django.http import HttpResponseRedirect
from social.pipeline.partial import partial
from django.utils.timezone import now
from linkedin.linkedin import (LinkedInDeveloperAuthentication, LinkedInApplication)
from linkedin.utils import raise_for_error
import re
import requests
from io import BytesIO
from access_check import check_accounts_access, check_to_send_email, get_user_tier_value
from common import remove_non_ascii
from auto_pick_country_timezone import auto_pick_timezone_location_from_ip, get_client_ip
from emoji_dictionary import emoji_dict
from email2 import send_out_of_queued_posts_email, send_email_unknown_error, send_email_account_expiry, send_email_failed_post
import django_rq
from autosocial.apps.advertising.models import Shares
from autosocial.apps.analytics.save_data import savePostData
from celery import shared_task
from django.core import serializers
from autosocial.settings import IMAGES_TO_POST
from instagram.client import InstagramAPI
from instagram.bind import InstagramAPIError
from instagram_schedule_post import send_instagram_post_notification_to_mobile
from error_messages import error_messages_dict
from boto.s3.key import Key
from autosocial.settings import CLOUD_ACCESS_KEY_ID_FOR_IMAGE_UPLOAD, CLOUD_SECRET_ACCESS_KEY_FOR_IMAGE_UPLOAD
import boto
import os
from requests_toolbelt import MultipartEncoder
import json
import analytics

logger = logging.getLogger(__name__)


def download_video_from_s3(key, bucket_name, title):
    BUCKET_NAME = 'druvideo'
    CLOUD_SECRET_ACCESS_KEY = CLOUD_SECRET_ACCESS_KEY_FOR_IMAGE_UPLOAD
    CLOUD_ACCESS_KEY_ID = CLOUD_ACCESS_KEY_ID_FOR_IMAGE_UPLOAD
    conn= boto.connect_s3(CLOUD_ACCESS_KEY_ID,CLOUD_SECRET_ACCESS_KEY)
    bucket= conn.get_bucket(BUCKET_NAME)
    k=Key(bucket)
    k.key = key
    k.get_contents_to_filename(str(k.key))

def upload_video_to_facebook(key, bucket_name, pa, title):
    download_video_from_s3(key, bucket_name, title)
    page_id = pa.page_id
    local_video_file = key
    video_file_name = key

    path = "{0}/videos".format(page_id)
    access_token = pa.token
    fb_url = "https://graph-video.facebook.com/{0}?access_token={1}".format(path, access_token)
    m = MultipartEncoder(
            fields={'description': title,
                    'title': title,
                    'source': (video_file_name, open(local_video_file, 'rb'))}
        )
    res = requests.post(fb_url, headers={'Content-Type': m.content_type}, data=m)
    os.remove(key)
    return res.json()

def upload_video_to_twitter(key, bucket_name, usr_twython, title):
    download_video_from_s3(key, bucket_name, title)
    local_video_file = key
    video_file_name = key
    video = open(local_video_file, 'rb')
    file_extenstion = key.split('.')[-1]
    media = usr_twython.upload_video(media=video, media_type='video/'+ file_extenstion)
    res = usr_twython.update_status(status=title, media_ids=[media['media_id']])
    os.remove(key)
    return res

def update_social_accounts(user, request):
    for uauth in UserSocialAuth.objects.filter(user=user):
        if (uauth.extended.last_api_sync > now()-timedelta(minutes=180) or not PostingAccounts.objects.filter(usersocialauth=uauth).filter(status__in=['enabled','disabled','hidden']).exists()):
            continue
        if uauth.provider == 'facebook':
            # refresh_fb_accounts(uauth,request)
            refresh_fb_account_full(uauth, request)
        elif uauth.provider == 'twitter':
            refresh_twitter_account(uauth, request)
        elif uauth.provider == 'linkedin' or uauth.provider == 'linkedin-oauth2':
            refresh_linkedin_account_full(uauth, request)
        elif uauth.provider == 'instagram':
            refresh_instagram_account(uauth, request)
        uauth.extended.last_api_sync = now()
        uauth.extended.save()


# Auto pick country and time zone only for the first account the user adds
# For second account and so the time zone and country are being copied from the first account
def check_to_auto_pick_country_timezone(request, uauth, account):
    return
    if not account.post_themes and PostingAccounts.objects.filter(usersocialauth__user=uauth.user).count() == 1:
        ip = get_client_ip(request)
        auto_pick_timezone_location_from_ip.apply_async(args=[ip, account.id], queue='high')
        #q = django_rq.get_queue('high')
        #q.enqueue(auto_pick_timezone_location_from_ip, ip, account)


# If post created/shared is older than 60 days then don't publish image
def check_to_publish_image_for_custom_posts(post):
    # For DrumUp v2.0
    img = post.uploaded_image_url if post.uploaded_image_url else ''
    # if not('aso3/' in img or 'aso5/' in img or 'giphy.com/' in img):
    if not any(x in img for x in IMAGES_TO_POST):
        publish_img = False
        return publish_img
    # For DrumUp v2.1 and V2.0
    publish_img = True
    if post.created_time < (now() - timedelta(days=120)):
        publish_img = False
    if publish_img:
        ea_share = EAPostShares.objects.filter(sch_id=post.id)
        if ea_share and ea_share[0].admin_publish_time < (now() - timedelta(days=59)):
            publish_img = False
    return publish_img


def check_add_via(user, post_id):
    date_joined = user.date_joined.date()
    add_via = False
    if date_joined < (now() - timedelta(days=14)).date():
        tier = get_user_tier_value(user)
        if (tier == 1) and (post_id % 2 == 0):
            add_via = True
    return add_via


def check_add_courtesy(user):
    tier = user.usersettings.tier
    add_courtesy = False
    if tier == 21:
        add_courtesy = True
    return add_courtesy


def calculate_message_length(msg):
    url_present = False
    if 'http' in msg:
        url_present = True
    msg = re.sub(r'http\S+', '', msg)
    msg_len = len(msg)
    if url_present: return msg_len + 23
    else: return msg_len

@shared_task()
def publish_post(post_id, post_type):
    if post_type == 0:
        post = Buffer.objects.get(id=post_id)
    elif post_type == 2:
        post = BufferUserFeed.objects.get(id=post_id)
    else:
        post = CustomPosts.objects.get(id=post_id)
    #post = serializers.deserialize("json", post).next().object
    account = post.account
    if (account.status != 'enabled' and account.status != 'hidden') or post.published or not post.queued:
        set_post_status_failed(post)
        return
    # for Instagram post scheduling - send notification
    # as third party apps can't publish on user behalf
    if account.usersocialauth.provider == "instagram":
        return send_instagram_post_notification_to_mobile(post)
    this_user = account.usersocialauth.user
    add_via = check_add_via(this_user, post.id)
    add_courtesy = check_add_courtesy(this_user) if not add_via else False
    if hasattr(post,'text'):
        msg=post.text
        #url = post.url
        url = post.url if post.url else ""
        img = post.uploaded_image_url if post.uploaded_image_url else None
        # Don't publish image if the post is 60 days old.
        if img and not check_to_publish_image_for_custom_posts(post):
            img = None
    else:
        img = None
        if post.uploaded_image_url:
            img = post.uploaded_image_url
        story = post.story if post.story else post.story_user_feed
        # Don't publish image if the post is 60 days old.
        if story.created_time < (now() - timedelta(days=59)):
            img = None
        if account.usersocialauth.provider=='twitter':
            msg = post.edited_title if len(post.edited_title)>1 else story.title[:110]
        if account.usersocialauth.provider=='facebook':
            msg = post.edited_title if len(post.edited_title)>1 else story.title[:470]
        if account.usersocialauth.provider in ['linkedin', 'linkedin-oauth2']:
            msg = post.edited_title if len(post.edited_title)>1 else story.title[:470]
        #url = story.url
        url = story.url if story.url else ""
    if len(msg) > 3 or img or url:
        # removing &nbsp; if exists. It exists in few posts
        msg = msg.replace(u'\xa0', ' ')
        url = url.replace(u'\xa0', ' ')
        msg = msg.encode("utf-8")
        url = url.encode("utf-8")
        msg = msg.replace('<div>', ' ')
        msg = msg.replace('</div>', ' ')
        auto_add_text = AutoAddTextAccount.objects.filter(account=account)
        if auto_add_text.exists():
            auto_add_text = auto_add_text.first().text.encode('utf-8')
            msg = msg + ' ' + auto_add_text
            msg_len = calculate_message_length(msg)
            if msg_len > 280 and account.usersocialauth.provider == 'twitter':
                while (msg_len > 280):
                    msg = msg.split('#')[:-1]
                    msg = '#'.join(msg)
                    msg_len = calculate_message_length(msg)
            msg = msg.strip().strip(',')
        if account.usersocialauth.provider=='twitter':
            try:
                if hasattr(post, 'uploaded_video_url'):
                    video = post.uploaded_video_url
                else: video = None
                res = publish_on_twitter(account,msg,url,img,add_via,add_courtesy, video)
                post_published_true(post)
                check_to_update_eashares(this_user, post, res)
                check_to_update_promoted_post_shares(account, post)
                update_social_id(account,post, res)
            except Exception as e:
                retried = post_published_false(post)
                if retried:
                    process_twitter_error(account, e)
                    logger.info("Could not pubslish on Twitter for user: " + str(account.name) + " Error: " + str(e))
                    if not post_type in [0,2]:
                        post = CustomPosts.objects.get(id=post_id)
                        FailedPosts.objects.create(post=post, message=str(e))
                        send_email_failed_post(post, str(e))

        elif account.usersocialauth.provider=='facebook':
            try:
                if hasattr(post, 'uploaded_video_url'):
                    video = post.uploaded_video_url
                else: video = None
                res = publish_on_facebook(account,msg,url,img,add_via,add_courtesy, video)
                post_published_true(post)
                check_to_update_eashares(this_user, post, res)
                check_to_update_promoted_post_shares(account, post)
                update_social_id(account,post, res)
            except Exception as e:
                retried = post_published_false(post)
                if retried:
                    process_fb_error(account.usersocialauth, e)
                    logger.info("Could not pubslish on Facebook for user: " + str(account.name) + " Error: " + str(e))
                    if not post_type in [0,2]:
                        post = CustomPosts.objects.get(id=post_id)
                        FailedPosts.objects.create(post=post, message=str(e))
                        send_email_failed_post(post, str(e))

        elif account.usersocialauth.provider in ['linkedin', 'linkedin-oauth2']:
            try:
                #res = publish_on_linkedin(account,msg,url,add_via,add_courtesy)
                if hasattr(post, 'story_type'): story_type = post.story_type
                else: story_type = None
                if account.linkedin_version == 0:
                    res = publish_on_linkedin(account,msg,url,img,add_via,add_courtesy, story_type)
                else:
                    res = publish_on_linkedin_v2(account,msg,url,img,add_via,add_courtesy, story_type)
                post_published_true(post)
                check_to_update_eashares(this_user, post, res)
                check_to_update_promoted_post_shares(account, post)
                update_social_id(account,post, res)
            except Exception as e:
                retried = post_published_false(post)
                if retried:
                    process_linkedin_error(account.usersocialauth, e)
                    logger.info("Could not pubslish on linkedin for user: " + str(account.name) + " Error: " + str(e))
                    if not post_type in [0,2]:
                        post = CustomPosts.objects.get(id=post_id)
                        FailedPosts.objects.create(post=post, message=str(e))
                        send_email_failed_post(post, str(e))
        check_to_send_out_of_queued_posts_email(post.account)


def check_to_send_out_of_queued_posts_email(account):
    user = account.usersocialauth.user
    email_pref = EmailPreferences.objects.get(user=user)
    sch_count = (Buffer.objects.filter(account=account.pk).filter(published=False).filter(relevance__gt=0).filter(published_time__isnull=False).count() +
        BufferUserFeed.objects.filter(account=account.pk).filter(published=False).filter(published_time__isnull=False).count() +
        CustomPosts.objects.filter(account=account.pk).filter(published=False).filter(published_time__isnull=False).count())
    if sch_count > 0:
        return
    if not email_pref.out_of_post_time_stamp or (email_pref.out_of_post_time_stamp < (now() - timedelta(hours=24))):
        if user.email and check_to_send_email(user, "acc_comm"):
            email_pref.out_of_post_time_stamp = now()
            email_pref.save()
            #q = django_rq.get_queue('serial_rq')
            send_out_of_queued_posts_email.apply_async(args=[user.id, account.id], queue='serial_rq')
            #q.enqueue(send_out_of_queued_posts_email, user, account)
            #send_out_of_queued_posts_email(user, account)


def post_published_true(post):
    post.published=True
    post.queued=False
    post.status = 'ok'
    post.published_time=datetime.utcnow().replace(tzinfo=timezone.utc)
    post.save()


def post_published_false(post):
    if post.status == 'ok':
        post.published=False
        post.queued=False
        post.status = 'retry'
        post.save()
        return False
    else:
        set_post_status_failed(post)
        return True


def set_post_status_failed(post):
    post.queued = False
    post.status = 'failed'
    post.save()


def check_to_add_via_courtesy(text, img):
    if ('drumup.io' in text) or ('@drumupio' in text):
        return False
    urls = re.findall(r'(https?://\S+)', text)
    len_text = len(text)
    for url in urls:
        len_text = len_text - len(url) + 23
    """
    if img:
        len_text += 24
    """
    if len_text > 245:
        return False
    else:
        return True

'''
def check_to_update_eashares(user, post, postId = None):
    ea_user = EAUsers.objects.filter(user=user)
    if ea_user and ea_user.first().user_type == 'member':
        if EAShares.objects.filter(sch_id=post.id, published=False).exists():
            share = EAShares.objects.filter(sch_id=post.id, published=False).first()
            share.published = True
            if postId != None:
                share.postId = postId
            share.published_time = now()
            share.save()
            """
            eauser = ea_user.first()
            eauser.monthly_shares += 1
            eauser.total_shares += 1
            eauser.save()
            """
'''

def check_to_update_eashares(user, post, postId = None):
    if hasattr(post, 'text'):
        ea_user = EAUsers.objects.filter(user=user)
        if ea_user and ea_user.first().user_type == 'member':
            ea_post_share = EAPostShares.objects.filter(sch_id=post.id, published=False)
            if ea_post_share:
                share = ea_post_share.first()
                share.published = True
                if postId != None:
                    share.postId = postId
                share.published_time = now()
                share.save()


def check_to_update_promoted_post_shares(account, post):
    if hasattr(post, 'text'):
        if Shares.objects.filter(account=account, custom_id=post.id, published=False).exists():
            share = Shares.objects.filter(account=account, custom_id=post.id).first()
            share.published = True
            share.save()

def update_social_id(account, post, res):
    if res:
        if hasattr(post,'text'):
            post_type = 1
        elif post.story:
            post_type = 0
        elif post.story_user_feed:
            post_type = 2
        post_social_id = PostSociaMediaId.objects.create(post_id=post.id,post_social_id=res,post_type=post_type,account=account)


def add_emoji_to_msg(msg):
    all_words = msg.split(':')
    new_word_list = []
    new_msg = ''
    for key in emoji_dict.keys():
        new_key = ':{}:'.format(key)
        msg = msg.replace(new_key,emoji_dict[key].encode('utf-8'))

    '''for word in all_words:
        if word in emoji_dict.keys():
            unicode_value = emoji_dict[word]
            new_word = '{}'.format(unicode_value.encode('utf-8'))
        else:
            new_word = ':{}'.format(word)
        new_word_list.append(new_word)
        new_msg += '{}'.format(new_word)
    return new_msg'''
    return msg


# For EA posts, clip post to 140 chars (for auto approve or 1-click schedule of EA posts having more than 140 chars)
def ea_clip_post_to_140char_for_twitter(account, msg):
    received_text = msg
    if not EAUsers.objects.filter(user=account.usersocialauth.user, user_type='member', status="enabled").exists():
        return received_text.strip()
    urls = re.findall(r'(https?://[^\s]+)', received_text)
    if len(urls):
        text = received_text
        for url in urls:
            text = text.replace(url, '')
        text = ' '.join(text.split())
        post_length = len(text) + len(urls) * 24
        if post_length <= 280:
            final_text = ' '.join(received_text.split())
        else:
            final_text = text[:253].strip() + '.. ' + urls[0].strip()
    else:
        if len(received_text.strip()) > 280:
            final_text = received_text.strip()[:278] + '..'
        else:
            final_text = received_text.strip()[:280]
    return final_text


def publish_on_twitter(account,msg,url,img,add_via,add_courtesy, video=None):
    # todo - consider twitter rate limits here. currently in case of exception, we just assume published
    '''DRUMUP CONFIDENTIAL CODE'''

def publish_on_facebook(account,msg,url,img,add_via,add_courtesy, video):
    '''DRUMUP CONFIDENTIAL CODE'''

def push_post_to_linkedin_v2(access_token, account, new_title, post_descr, post_url, post_image_url=False):
    url = 'https://api.linkedin.com/v2/shares'
    if account.account_type == 'page':
        post_owner = "urn:li:organization:{}".format(account.page_id)
    else:
        post_owner = "urn:li:person:{}".format(account.page_id)
    if post_image_url:
        data = {
            "content": {
                "contentEntities": [
                    {
                        "entityLocation": post_url,
                        "thumbnails": [
                            {
                                "resolvedUrl":post_image_url
                            }
                        ]
                    }
                ],
                #"title": new_title
            },
            "distribution": {
                "linkedInDistributionTarget": {}
            },
            "owner": post_owner,
            "text": {
                "text": post_descr
            }
        }
    else:
        data = {
            "content": {
                "contentEntities": [
                    {
                        "entityLocation": post_url
                    }
                ],
                #"title": new_title
            },
            "distribution": {
                "linkedInDistributionTarget": {}
            },
            "owner": post_owner,
            "text": {
                "text": post_descr
            }
        }
    data = json.dumps(data)
    headers = {'x-li-format': 'json','Content-Type': 'application/json'}
    params = {}
    kw = dict(data=data, params=params, headers=headers, timeout=60)
    params.update({'oauth2_access_token': access_token})
    res = requests.request('POST', url, **kw)
    return res.json()


def publish_on_linkedin_v2(account,msg,url,img,add_via,add_courtesy, story_type=None):
    '''DRUMUP CONFIDENTIAL CODE'''

def publish_on_linkedin(account,msg,url,img,add_via,add_courtesy, story_type=None):
    if add_via:
        msg += ' via drumup.io'
    elif add_courtesy:
        msg += ' courtesy drumup.io'
    if len(url) > 3:
        msg = msg.strip()
        check = check_for_url(msg)
        if not check[2] and len(check[1]) == 0:
            msg += ' ' + url
    user_linkedin = get_user_linkedin(account)
    msg = add_emoji_to_msg(msg)
    # if image url is valid then publsh post with image else publish just the text
    if img and requests.get(url=img).status_code != 200:
        img = None
    if img:
        urls = re.findall(r'(https?://[^\s]+)', msg)
        if not urls: #handling edge case when no https is present in the url.
            if '.com' in msg:
                splited_text = msg.split('.com')
                urls = ['https://www.' + splited_text[0].split()[-1] + '.com' + splited_text[1].split()[0]]
        if not urls:
            post_text = msg.strip()
            post_url = img
        else:
            post_url = urls[0]
            post_text = msg.replace(post_url, '').strip()

        if not post_text:
            post_text = post_url
            post_comment = None
            new_title = post_text
        else:
            try:
                post_comment = post_text
                new_title = post_text + ' ' + post_url
            except UnicodeDecodeError:
                post_comment = post_text
                new_title = post_text + ' ' + post_url.encode('utf-8')
        submitted_post_image_url = img
        submitted_post_url = post_url
        post_title = post_text[:200]
        post_descr = post_text[:256]
        if story_type == 6:
            if account.account_type == 'page':
                res = user_linkedin.submit_company_share(company_id=str(account.page_id),comment=new_title,title=new_title,description=post_descr)
            else:
                res = user_linkedin.submit_share(comment=new_title,title=new_title,description=post_descr)
        else:
            if account.account_type == 'page':
                res = user_linkedin.submit_company_share(company_id=str(account.page_id),comment=post_comment,title=post_title,description=post_descr,submitted_url=submitted_post_url,submitted_image_url=submitted_post_image_url)
            else:
                res = user_linkedin.submit_share(comment=post_comment,title=post_title,description=post_descr,submitted_url=submitted_post_url,submitted_image_url=submitted_post_image_url)
    else:
        if account.account_type == 'page':
            res = user_linkedin.submit_company_share(str(account.page_id),msg)
        else:
            res = user_linkedin.submit_share(msg)
    return res['updateKey']


def check_for_url(input_string):
    occurence_list = re.findall(r'(https?://\S+)', input_string)
    if not len(occurence_list):
        return input_string, "", False
    link = occurence_list[len(occurence_list)-1]
    endswith = input_string.endswith(link)
    if endswith == True:
        input_string = input_string[:-1*len(link)]
    return input_string, link, endswith


def process_instagram_error(uauth, e):
    excep = str(e)
    if 'OAuthAccessTokenException' in excep:
        pa = PostingAccounts.objects.filter(usersocialauth=uauth)
        for p in pa:
            p.status = 'revoked'
            p.save()
        user_id = uauth.user.id
        logger.error('User_id ' + str(user_id) + ' seems to have revoked Instagram access. Setting account status as revoked.')
    elif '400' in excep:
        pa = PostingAccounts.objects.filter(usersocialauth=uauth)
        for p in pa:
            p.status = 'expired'
            p.save()
            send_email_account_expiry(p)
        user_id = uauth.user.id
        logger.error('User_id ' + str(user_id) + ' detected spammy behavior. Setting account status as expired.')


def process_twitter_error(account, e):
    if '401' in str(e):
        # account.status = 'revoked'
        account.status = 'expired'
        #account.post_themes = None
        #account.negative_themes = None
        #account.processed_themes = None
        #account.posts_per_day = 3
        #account.time_zone = 'EST'
        #account.location = 'us'
        account.save()
        send_email_account_expiry(account)
        Buffer.objects.filter(account=account).delete()
        account.usersocialauth.user.usersettings.last_active_account = 0
        account.usersocialauth.user.usersettings.save()
        logger.error(str(account.id) + ' seems to have revoked Twitter access. Disabling postingaccount')
    elif '403' in str(e) and 'to protect our users from spam and other malicious activity' in str(e).lower():
        account.status = 'expired'
        account.save()
        send_email_account_expiry(account)
    elif '403' in str(e) and 'status is over 140 characters' in str(e).lower():
        logger.error(str(account.id) + ' Twitter error - Char count exceeded: ' + str(e))
    else:
        logger.error(str(account.id) + ' twitter access error: ' + str(e))

def process_linkedin_error(uauth, e):
    if '401' in str(e):
        for p in PostingAccounts.objects.filter(usersocialauth=uauth):
            # p.status = 'revoked'
            p.status = 'expired'
            send_email_account_expiry(p)
            p.post_themes = None
            p.negative_themes = None
            p.processed_themes = None
            p.posts_per_day = 3
            p.time_zone = 'EST'
            p.location = 'us'
            p.save()
            Buffer.objects.filter(account=p).delete()
        uauth.user.usersettings.last_active_account = 0
        uauth.user.usersettings.save()
        logger.error(uauth.user.username + ' seems to have revoked linkedin access. Disabling postingaccount')
    elif 's_412_precondition_failed' in str(e).lower():
        for p in PostingAccounts.objects.filter(usersocialauth=uauth):
            p.status = 'expired'
            send_email_account_expiry(p)
            p.save()
        uauth.user.usersettings.last_active_account = 0
        uauth.user.usersettings.save()
        logger.error(uauth.user.username + ' seems to have trouble accessing API. Disabling postingaccounts')
    else:
        logger.error(uauth.user.username + ' linkedin access error: ' + str(e))

def process_fb_error(uauth,e):
    # todo: will have to check for genuine token expiry here and request extension
    if 'user has not authorized application *************' in str(e):
        for p in PostingAccounts.objects.filter(usersocialauth=uauth):
            # p.status = 'revoked'
            p.status = 'expired'
            p.post_themes = None
            p.negative_themes = None
            p.processed_themes = None
            p.posts_per_day = 3
            p.time_zone = 'EST'
            p.location = 'us'
            p.save()
            send_email_account_expiry(p)
            Buffer.objects.filter(account=p).delete()
        uauth.user.usersettings.last_active_account = 0
        uauth.user.usersettings.save()
        logger.error(uauth.user.username + ' seems to have revoked Facebook access. Disabling postingaccount')
    elif 'session has been invalidated because the user has changed the password' in str(e).lower():
        set_fb_account_expired(uauth)
        logger.error(uauth.user.username + ' seems to have changed Facebook password. Setting account status as expired.')
    elif 'no json object could be decoded' in str(e).lower():
        set_fb_account_expired(uauth)
        logger.error(uauth.user.username + ' JSON object could not be decoded. Setting account status as expired.')
    elif "the user hasn't authorized the application" in str(e).lower():
        set_fb_account_expired(uauth)
        logger.error(uauth.user.username + ' has not authorized app. Setting account status as expired.')
    elif "error validating access token" in str(e).lower():
        set_fb_account_expired(uauth)
        logger.error(uauth.user.username + ' failed validating access token. Setting account status as expired.')
    elif "'manage_pages' permission must be granted" in str(e).lower():
        set_fb_account_expired(uauth)
        logger.error(uauth.user.username + ' Manage page permission not given. Setting account status as expired.')
    elif "user must be an administrator of the page" in str(e).lower():
        set_fb_account_expired(uauth)
        logger.error(uauth.user.username + ' User is not admin of the page. Setting account status as expired.')
    elif "permissions error" in str(e).lower():
        set_fb_account_expired(uauth)
        logger.error(uauth.user.username + ' Permissions error. Setting account status as expired.')
    elif "an unknown error has occured" in str(e).lower():
        set_fb_account_expired(uauth)
        logger.error(uauth.user.username + ' an unknown error occured. Setting account status as expired.')
    else:
        logger.error(uauth.user.username + ' Unknown facebook access error: ' + str(e))


def set_fb_account_expired(uauth):
    for p in PostingAccounts.objects.filter(usersocialauth=uauth):
        p.status = 'expired'
        p.save()
        send_email_account_expiry(p)


def get_timeline(account):
    usr_twython = get_user_twython(account)
    try:
        timeline = usr_twython.get_home_timeline(count=50,trim_user=True,contributor_details=False,exclude_replies=True)
    except TwythonAuthError as e:
        process_twitter_error(account, e)
    except Exception as e:
        logger.error("Could not connect with Twitter for user: " + str(account.name) + " Error: " + str(e))
    else:
        return timeline


def get_tz_country(uauth):
    accounts = PostingAccounts.objects.filter(usersocialauth__user=uauth.user)
    if accounts:
        pa = accounts[0]
        tz = pa.time_zone
        country = pa.location
    else:
        tz = 'US/Pacific'
        country = 'us'
    return tz, country


def get_tz_country_v2(settings):
    time_zone = 'US/Pacific'
    country = 'us'
    if settings.location:
        country = settings.location
    if settings.time_zone:
        time_zone = settings.time_zone
    return time_zone, country


def add_posting_account(uauth, id, request, social_account_name=None):
    '''DRUMUP CONFIDENTIAL CODE'''

def copy_linkedin_v1_posts_to_v2_account(account, linkedin_v1_account):
    for post in CustomPosts.objects.filter(account=linkedin_v1_account, published=False, published_time__gte=now()):
        post.account = account
        post.save()

def get_account_already_added_message(drumup_version, account, account_provider):
    if drumup_version == 1:
        if account.post_themes and account_provider == 'twitter':
            social_provider = 'Twitter'
            social_link = 'https://www.twitter.com/'
        else:
            social_provider = ''
            social_link = ''
    else:
        if account_provider == 'twitter':
            social_provider = 'Twitter'
            social_link = 'https://www.twitter.com/'
        elif account_provider == 'facebook':
            social_provider = 'Facebook'
            social_link = 'https://www.facebook.com/'
        elif account_provider == 'linkedin':
            social_provider = 'LinkedIn'
            social_link = 'https://www.linkedin.com/'
        else:
            social_provider = ''
            social_link = ''
    if social_provider == '':
        return False
    msg = 'Logged in '+social_provider+' account already connected! - Looking to add another account? \
                   Log out of this <a href="'+social_link+'" target="_blank">'+social_provider+' account</a> and try again.'
    message = [{'text': msg, 'level': 'danger', 'persistent': False}]
    return message


def fix_broken_fb_username(uauth):
    # if the user already has a posting account, then a name would have already been created
    # username generated from fb would be 30 chars long
    if len(uauth.user.username) < 29 or PostingAccounts.objects.filter(usersocialauth=uauth).exists():
        return
    user_info = fb_request(uauth, 'me')
    #name_fb = getattr(user_info, 'name', '')
    name_fb = user_info.get('name', '')
    if name_fb == '':
        uauth.user.username = uauth.user.email.split('@')[0][:29]
        uauth.user.save()

def fix_broken_linkedin_username(uauth):
    # if the user already has a posting account, then a name would have already been created
    # username generated from fb would be 30 chars long
    if len(uauth.user.username) < 29 or PostingAccounts.objects.filter(usersocialauth=uauth).exists():
        return
    uauth.user.username = uauth.user.username[:29]
    uauth.user.save()

def fb_request(uauth, str):
    graph = facebook.GraphAPI(uauth.extra_data['access_token'],version='2.12')
    try:
        user_info = graph.request(str)
    except Exception as e:
        process_fb_error(uauth,e)
        return False
    else:
        return user_info

def linkedin_request(uauth):
    '''DRUMUP CONFIDENTIAL CODE'''#try:

@partial
def user_password(*args, **kwargs):
    '''DRUMUP CONFIDENTIAL CODE'''

@partial
def update_accounts(*args, **kwargs):
    '''DRUMUP CONFIDENTIAL CODE'''


def get_fb_page_list(uauth):
    '''DRUMUP CONFIDENTIAL CODE'''

"""

def list_companies(uauth,params=None):
    lol={'is-company-admin':'True','format':'json'}
    url='https://api.linkedin.com/v1/companies'+':(id,name,square-logo-url)'
    app=linkedin_request(uauth)
    response = app.make_request('GET',url,params=lol)
    raise_for_error(response)
    return response.json()
"""

def get_linkedin_v2_page_list(uauth):
    '''DRUMUP CONFIDENTIAL CODE'''

def get_linkedin_page_list(uauth):
    page_list = []
    account = linkedin_request(uauth)
    try:
        user_info = account.get_profile(None,None,('firstName','id','pictureUrl','lastName'))
    except Exception as e:
        process_linkedin_error(uauth,e)
        return False
    else:
        if not user_info:
            return page_list
        individual_account = {
            'access_token': uauth.extra_data['access_token'],
            'category': 'individual_x',
            'name': user_info['firstName'] + ' ' + user_info['lastName'],
            'img_url': (user_info['pictureUrl'] if 'pictureUrl' in user_info else 'https://drumup.io/static/images/linkedin_profile_pic_blank.png'),
            'id': uauth.uid}
        page_list.append(individual_account)
        response = account.make_request(
            'GET','https://api.linkedin.com/v1/companies'+':(id,name,square-logo-url)',
            params={'is-company-admin':'True','format':'json','start':'0','count':'20'})
        try:
            raise_for_error(response)
        except:
            response = account.make_request(
            'GET','https://api.linkedin.com/v1/companies'+':(id,name)',
            params={'is-company-admin':'True','format':'json','start':'0','count':'20'})
        raise_for_error(response)
        resp = response.json()
        if not 'values' in resp:
            return page_list
        for company in response.json()['values']:
            page = {
                'access_token': uauth.extra_data['access_token'],
                'category': 'page',
                'name': company['name'],
                'img_url': (
                    company['squareLogoUrl'] if 'squareLogoUrl' in company
                    else 'https://drumup.io/static/images/linkedin_company_pic_blank.png'),
                'id': str(company['id'])}
            page_list.append(page)
        #print page_list
        return page_list

def set_account_expired_session(request, uauth, provider, expire_account):
    account_expired = get_account_expired_session_var(provider)
    for p in PostingAccounts.objects.filter(usersocialauth=uauth, status__in=['enabled', 'disabled', 'hidden']):
        request.session[account_expired] = p.name
        if expire_account:
            p.status = 'expired'
            p.save()
            send_email_account_expiry(p)


def reset_account_expired_session(request, provider):
    account_expired = get_account_expired_session_var(provider)
    # request.session['linkedin_expired'] = 0
    added_expiry = request.session.get(account_expired, False)
    if not added_expiry:
        request.session[account_expired] = 0


def get_account_expired_session_var(provider):
    if provider == 'facebook':
        account_expired = 'facebook_expired'
    elif provider == 'linkedin':
        account_expired = 'linkedin_expired'
    elif provider == 'instagram':
        account_expired = 'instagram_expired'
    return account_expired


def check_to_reset_account_expiry_message(request, uauth, provider):
    account_expired = get_account_expired_session_var(provider)
    if uauth.extended.last_token_sync > now() - timedelta(hours=1):
        request.session[account_expired] = 0


def check_expiry_linkedin(uauth, request):
    provider = 'linkedin'
    if uauth.extended.last_token_sync < now()-timedelta(days=60):
        set_account_expired_session(request, uauth, provider, True)
        return True
    else:
        reset_account_expired_session(request, provider)
        # return False
    check_to_reset_account_expiry_message(request, uauth, provider)
    return False


def check_revoke_before_refresh_linkedin(uauth, request):
    provider = 'linkedin'
    if uauth.extended.last_token_sync < now()-timedelta(days=40):
        set_account_expired_session(request, uauth, provider, False)
        # return True
        return False
    else:
        reset_account_expired_session(request, provider)
        # return False
    check_to_reset_account_expiry_message(request, uauth, provider)
    return False


def check_expiry_facebook(uauth, request):
    provider = 'facebook'
    if uauth.extended.last_token_sync < now()-timedelta(days=60):
        set_account_expired_session(request, uauth, provider, True)
        return True
    else:
        reset_account_expired_session(request, provider)
        # return False
    check_to_reset_account_expiry_message(request, uauth, provider)
    return False


def check_revoke_before_refresh_facebook(uauth, request):
    provider = 'facebook'
    if uauth.extended.last_token_sync < now()-timedelta(days=40):
        set_account_expired_session(request, uauth, provider, False)
        # return True
        return False
    else:
        reset_account_expired_session(request, provider)
        # return False
    check_to_reset_account_expiry_message(request, uauth, provider)
    return False


def check_fb_pswd_change(uauth,request):
    graph = facebook.GraphAPI(uauth.extra_data['access_token'],version='2.12')
    try:
        check_user_info = graph.request('me')
    except Exception as e:
        process_fb_error(uauth, e)
        return True
    else:
        request.session['fb_pswd_change'] = 0
        return False
    """
    except Exception as e:
        if 'session has been invalidated because the user has changed the password' in str(e):
            request.session['fb_pswd_change'] = 1
            return True
    """


def check_expiry_instagram(uauth, request):
    provider = 'instagram'
    if uauth.extended.last_token_sync < now()-timedelta(days=60):
        set_account_expired_session(request, uauth, provider, True)
        return True
    else:
        reset_account_expired_session(request, provider)
        # return False
    check_to_reset_account_expiry_message(request, uauth, provider)
    return False


def check_revoke_before_refresh_instagram(uauth, request):
    provider = 'instagram'
    if uauth.extended.last_token_sync < now()-timedelta(days=40):
        set_account_expired_session(request, uauth, provider, False)
        # return True
        return False
    else:
        reset_account_expired_session(request, provider)
        # return False
    check_to_reset_account_expiry_message(request, uauth, provider)
    return False


def get_clean_name(uauth, name):
    name = remove_non_ascii(name)
    if not name:
        name = uauth.user.email.split('@')[0][:29]
    return name


def save_clean_username(uauth):
    name = get_clean_name(uauth, uauth.user.username)
    uauth.user.username = name
    uauth.user.save()


####### The below functions are for Keyword Suggestion ##########

def get_fb_info_posts_statuses(pa, want_info=True, want_posts=True,want_statuses=True):
    graph = facebook.GraphAPI(pa.token,version='2.12')
    page_info = False
    page_posts = False
    page_statuses = False
    try:
        if want_info:
            page_info = graph.request('me')
        if want_posts:
            page_posts = graph.request('me/posts')
        if want_statuses:
            page_statuses = graph.request('me/statuses')

    except Exception as e:
        if 'user has not authorized application **************' in str(e):
            # pa.status = 'revoked'
            pa.status = 'expired'
            pa.save()
            send_email_account_expiry(pa)
            logger.info(str(pa.id) + ' seems to have revoked Facebook access. Disabling postingaccount')
        elif 'Permissions error' in str(e):
            pa.status = 'does not exist'
            pa.save()
            logger.info(str(pa.id) + ' seems to have deleted the page.')
        else:
            logger.info(str(pa.id) + ' facebook access error: ' + str(e))
        return False, False, False

    return page_info, page_posts, page_statuses


def get_twitter_account(uauth):
    account = PostingAccounts.objects.get(usersocialauth=uauth)
    user_twython = get_user_twython(account)
    user_info = False
    try:
        user_info = user_twython.verify_credentials()
    except TwythonAuthError as e:
        process_twitter_error(account, e)
        return False
    except Exception as e:
        logger.error("Could not connect with Twitter for user: " + str(account.name) + " Error: " + str(e))
        return False
    else:
        return user_info


def get_detailed_timeline(account,home_count=50,user_count=50):
    usr_twython = get_user_twython(account)
    home_timeline = False
    user_timeline = False
    try:
        home_timeline = usr_twython.get_home_timeline(count=home_count,trim_user=False,contributor_details=False,exclude_replies=True)
    except TwythonAuthError as e:
        process_twitter_error(account, e)
    except Exception as e:
        logger.error("Could not connect with Twitter for user: " + str(account.name) + " Error: " + str(e))
    try:
        user_timeline = usr_twython.get_user_timeline(user_id=account.page_id,count=user_count,trim_user=False,contributor_details=False,exclude_replies=True)
    except TwythonAuthError as e:
        process_twitter_error(account, e)
    except Exception as e:
        logger.error("H Could not connect with Twitter for user: " + str(account.name) + " Error: " + str(e))
    return home_timeline, user_timeline


def get_linkedin_timeline(account):
    application = get_user_linkedin(account)
    try:
        if account.page_id.isdigit():
            return application.get_company_updates(str(account.page_id))
        else:
            return False
    except Exception as e:
        logger.error("Could not get linkedin timeline for user: " + str(account.name) + " Error: " + str(e))


def get_followers_count(account):
    if account.status != 'enabled':
        return -1
    if account.usersocialauth.provider != 'twitter':
        return -1
    user_twython = get_user_twython(account)
    user_info = user_twython.verify_credentials()
    if 'followers_count' not in user_info:
        return -1
    return user_info['followers_count'], user_infofriends_count

@shared_task()
def update_follow_counts(account, called_from_celery=False):
    if called_from_celery:
        account = serializers.deserialize("json", account).next().object
    if account.status != 'enabled' and account.status != 'hidden':
        return -1
    if account.usersocialauth.provider == 'twitter':
        # return -1
        try:
            user_twython = get_user_twython(account)
            user_info = user_twython.verify_credentials()
        except TwythonAuthError:
            return 0
        if 'followers_count' not in user_info or 'friends_count' not in user_info:
            return -1
        counts, created = FollowCounts.objects.get_or_create(account=account)
        counts.followers_count = user_info['followers_count']
        counts.following_count = user_info['friends_count']
        counts.save()
    elif account.usersocialauth.provider in ['linkedin', 'linkedin-oauth2'] and account.account_type == 'individual':
        user_linkedin = get_user_linkedin(account)
        res = user_linkedin.get_profile(selectors='num-connections')

        if 'numConnections' not in res:
            return -1

        counts, created = FollowCounts.objects.get_or_create(account=account)
        counts.followers_count = res['numConnections']
        counts.save()

    elif account.usersocialauth.provider in ['linkedin', 'linkedin-oauth2'] and account.account_type == 'page':
       user_linkedin = get_user_linkedin(account)
       res = user_linkedin.make_request('GET','https://api.linkedin.com/v1/companies/'+account.page_id+'/company-statistics').json()

       if 'followStatistics' not in res:
           return -1

       if 'count' not in res['followStatistics']:
           return -1

       counts, created = FollowCounts.objects.get_or_create(account=account)
       counts.followers_count = res['followStatistics']['count']
       counts.save()

    elif account.usersocialauth.provider == 'facebook' and account.account_type == 'individual':
        graph = facebook.GraphAPI(account.token,version=2.6)
        try:
            res = graph.request('me/friends', args={'fields':'friends.limit(0)'})
        except Exception as e:
            logger.info("Error update follow count: " + str(e) + ";\nAccount:" + str(account.id) + " " + str(account.name))
            return

        if 'summary' not in res:
            return -1
        if 'total_count' not in res['summary']:
            return -1

        counts, created = FollowCounts.objects.get_or_create(account=account)
        counts.followers_count = res['summary']['total_count']
        counts.save()

    elif account.usersocialauth.provider == 'facebook' and account.account_type == 'page':
       graph = facebook.GraphAPI(account.token,version=2.6)
       try:
           res = graph.request('/v{}/{}'.format(str(graph.get_version()), str(account.page_id)), args={'fields':'fan_count'})
       except Exception as e:
           logger.info('could not connect with facebook for page ', account.page_id, ' error is ', str(e))
           return

       if 'fan_count' not in res:
           return -1

       counts, created = FollowCounts.objects.get_or_create(account=account)
       counts.followers_count = res['fan_count']
       counts.save()


def follow_drumup(account):
    msg = 'Just connected with drumup.io, the world\'s #1 content marketing platform!'

    if account.usersocialauth.provider == 'twitter':
        msg = 'Just connected with https://drumup.io, the world\'s #1 content marketing platform!'
        try:
            acc_twython = get_user_twython(account)
            # Follow DrumUp
            follow_du = acc_twython.create_friendship(screen_name='drumupio', follow=False)
            # Post Init Msg
            img = 'https://s3-us-west-2.amazonCLOUD.com/drustatic/img/drumup_promo.jpg'
            photo = requests.get(url=img).content
            # acc_twython.update_status(status= msg)
            acc_twython.update_status_with_media(status=msg,media=BytesIO(photo))
        except Exception as e:
            logger.error("Could not follow DrumUp by twitter account: " + str(account.name) + " Error: " + str(e))

    elif account.usersocialauth.provider == 'facebook':
        msg = 'Just connected with https://drumup.io, the world\'s #1 content marketing platform!'
        try:
            graph = facebook.GraphAPI(account.token,version='2.12')
            # Post Init Msg
            res = graph.put_object(account.page_id, "feed", message=msg, link="https://drumup.io")
            if account.account_type == 'individual':
                savePostData(account,res['id'],msg)
        except Exception as e:
            logger.error("Could not follow DrumUp by facebook account: " + str(account.name) + " Error: " + str(e))

    elif account.usersocialauth.provider in ['linkedin', 'linkedin-oauth2']:
        try:
            user = get_user_linkedin(account)
            # Follow DrumUp Page
            user.follow_company('******')
            # Make init post
            if account.account_type == 'page':
                user.submit_company_share(str(account.page_id),msg)
            else:
                user.submit_share(msg)
        except Exception as e:
            logger.error("Could not follow DrumUp by linkedin account: " + str(account.name) + " Error: " + str(e))


def get_twitter_followers_following_count(account):
    try:
        user_twython = get_user_twython(account)
        user_info = user_twython.verify_credentials()
    except TwythonAuthError:
        return -1, -1
    if 'followers_count' not in user_info or 'friends_count' not in user_info:
        return -1, -1
    followers_count = user_info['followers_count']
    following_count = user_info['friends_count']
    return followers_count, following_count


def get_facebook_followers_following_count(account):
    account_type = account.account_type
    followers_count = -1
    if account_type == 'individual':
        graph = facebook.GraphAPI(account.token, version='2.12')
        try:
            res = graph.request('me/friends', args={'fields': 'friends.limit(0)'})
        except Exception as e:
            logger.info("Error update follow count: " + str(e) + ";\nAccount:" + str(account.id) + " " + str(account.name))
            return -1
        if 'summary' not in res:
            followers_count = -1
        elif 'total_count' not in res['summary']:
            followers_count = -1
        else:
            followers_count = res['summary']['total_count']
    elif account_type == 'page':
        graph = facebook.GraphAPI(account.token, version='2.12')
        try:
            res = graph.request('/v{}/{}'.format(str(graph.get_version()), str(account.page_id)), args={'fields': 'fan_count'})
        except Exception as e:
            logger.info('could not connect with facebook for page ', account.page_id, ' error is ', str(e))
            return
        if 'fan_count' not in res:
            followers_count = -1
        else:
            followers_count = res['fan_count']
    return followers_count


def get_linkedin_followers_following_count(account):
    account_provider = account.usersocialauth.provider
    account_type = account.account_type
    followers_count = -1
    if account_provider in ['linkedin', 'linkedin-oauth2'] and account_type == 'individual':
        user_linkedin = get_user_linkedin(account)
        res = user_linkedin.get_profile(selectors='num-connections')
        if 'numConnections' not in res:
            followers_count = -1
        else:
            followers_count = res['numConnections']
    elif account_provider in ['linkedin', 'linkedin-oauth2'] and account_type == 'page':
        user_linkedin = get_user_linkedin(account)
        res = user_linkedin.make_request('GET', 'https://api.linkedin.com/v1/companies/'+account.page_id+'/company-statistics').json()
        if 'followStatistics' not in res:
            followers_count = -1

        elif 'count' not in res['followStatistics']:
            followers_count = -1
        else:
            followers_count = res['followStatistics']['count']
    return followers_count


def get_instagram_followers_following_count(account):
    uauth = account.usersocialauth
    resp = get_user_instagram(uauth)
    insta_user = resp.user()
    followers_count = insta_user.counts['followed_by']
    following_count = insta_user.counts['follows']
    return followers_count, following_count


@shared_task()
def update_follow_counts(account, called_from_celery=False):
    if called_from_celery:
        account = serializers.deserialize("json", account).next().object
    if account.status != 'enabled' and account.status != 'hidden':
        return
    counts, created = FollowCounts.objects.get_or_create(account=account)
    account_provider = account.usersocialauth.provider
    if account_provider == 'twitter':
        followers_count, following_count = get_twitter_followers_following_count(account)
        if followers_count >= 0:
            counts.followers_count = followers_count
            counts.following_count = following_count
    elif account_provider == 'facebook':
        followers_count = get_facebook_followers_following_count(account)
        if followers_count >= 0:
            counts.followers_count = followers_count
    elif account_provider in ['linkedin', 'linkedin-oauth2']:
        followers_count = get_linkedin_followers_following_count(account)
        if followers_count >= 0:
            counts.followers_count = followers_count
    elif account_provider == 'instagram':
        followers_count, following_count = get_instagram_followers_following_count(account)
        if followers_count >= 0:
            counts.followers_count = followers_count
            counts.following_count = following_count
    counts.save()


@shared_task()
def update_follow_counts_on_adding_new_profile(account_id):
    account = PostingAccounts.objects.get(id=account_id)
    if account.status != 'enabled' and account.status != 'hidden':
        return
    counts, created = FollowCounts.objects.get_or_create(account=account)
    if not created:
        return
    account_provider = account.usersocialauth.provider
    if account_provider == 'twitter':
        followers_count, following_count = get_twitter_followers_following_count(account)
        if followers_count >= 0:
            counts.profile_added_followers_count = followers_count
            counts.following_count = following_count
    elif account_provider == 'facebook':
        followers_count = get_facebook_followers_following_count(account)
        if followers_count >= 0:
            counts.profile_added_followers_count = followers_count
    elif account_provider in ['linkedin', 'linkedin-oauth2']:
        followers_count = get_linkedin_followers_following_count(account)
        if followers_count >= 0:
            counts.profile_added_followers_count = followers_count
    elif account_provider == 'instagram':
        followers_count, following_count = get_instagram_followers_following_count(account)
        if followers_count >= 0:
            counts.profile_added_followers_count = followers_count
            counts.following_count = following_count
    counts.save()
