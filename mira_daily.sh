#!/bin/bash
# Daily @mirahaldane run (selfhelpist loop): engage (follow cap) -> unfollow (>=5 d non-followers, engager follows only) -> hashtag bot.
cd ~/Projects/insta-ai-engager
P=/tmp/mira_daily.progress
.venv/bin/python insta_targets.py mirahaldane-follow engage > /tmp/mira_engage.log 2>&1; echo "engage rc=$?" >> $P
.venv/bin/python insta_targets.py mirahaldane-follow unfollow > /tmp/mira_unfollow.log 2>&1; echo "unfollow rc=$?" >> $P
.venv/bin/python insta_bot.py mirahaldane > /tmp/mira_hashtag.log 2>&1; echo "hashtag rc=$?" >> $P
echo done >> $P
