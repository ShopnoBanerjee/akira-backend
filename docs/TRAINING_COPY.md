# Training walkthrough: every step, for the owner's review

Generated from `akira-frontend/src/features/training/content.ts` (P24, D31).
Wording is fixed in the app by the owner's choice; to change a line, name the
step and the new text. A change bumps that track's version; completions stay.

## Management tour (owner, ops manager, outlet manager)

### 1. Welcome to AKIRA Ops

- Centred card, no control highlighted
- **EN:** This is a short walk through the screens you will use. It takes about two minutes. Tap Next to move on; nothing you do here changes any data.
- **BN:** AKIRA Ops-এ স্বাগতম — আপনি যে স্ক্রিনগুলো ব্যবহার করবেন, তার একটি ছোট পরিচয়। প্রায় দুই মিনিট লাগবে। এগোতে Next চাপুন; এখানে কিছুতেই কোনো তথ্য বদলাবে না।

### 2. The outlet's health, every morning

- Points at: `dashboard-health`
- **EN:** One score from four pillars: checklists done, sales, stock and guests. Green, amber or red. Tap any pillar to see exactly which numbers made it.
- **BN:** প্রতিদিন সকালে আউটলেটের অবস্থা — চারটি স্তম্ভ থেকে একটি স্কোর: চেকলিস্ট, বিক্রি, স্টক ও অতিথি। সবুজ, হলুদ বা লাল। কোন সংখ্যা থেকে এল দেখতে যেকোনো স্তম্ভে চাপুন।

### 3. Review queue: your daily job

- Points at: `nav-review`
- **EN:** Every checklist staff submit lands here with its photos. You approve or send it back. A run you submitted yourself can never be approved by you.
- **BN:** রিভিউ কিউ: আপনার দৈনিক কাজ — স্টাফ যে চেকলিস্ট জমা দেয়, ছবিসহ এখানে আসে। আপনি অনুমোদন করেন বা ফেরত পাঠান। নিজের জমা দেওয়া রান নিজে অনুমোদন করা যায় না।

### 4. Exceptions: what went wrong

- Points at: `nav-exceptions`
- **EN:** Missed checklists, failed critical items, doubtful photos and stock anomalies gather here. Acknowledge them, assign them, resolve them with a note.
- **BN:** এক্সেপশন: কোথায় সমস্যা হল — মিস হওয়া চেকলিস্ট, ফেল করা জরুরি আইটেম, সন্দেহজনক ছবি ও স্টকের অসঙ্গতি এখানে জমা হয়। দেখুন, কাউকে দিন, নোটসহ সমাধান করুন।

### 5. Sales: upload the Petpooja exports

- Points at: `nav-sales`
- **EN:** Drop the Orders Master, Order Listing and Category reports here. The app checks the restaurant name, then builds attach rates, forecasts and the sales pillar from them.
- **BN:** বিক্রি: Petpooja এক্সপোর্ট আপলোড করুন — Orders Master, Order Listing ও Category রিপোর্ট এখানে দিন। অ্যাপ রেস্তোরাঁর নাম যাচাই করে, তারপর অ্যাটাচ রেট, পূর্বাভাস ও বিক্রির স্তম্ভ তৈরি করে।

### 6. Stock counts and requisitions

- Points at: `nav-stock-counts`
- **EN:** Photograph a count sheet; the app reads it and asks you to confirm each line. Confirmed counts feed consumption, anomalies and the requisition list.
- **BN:** স্টক গণনা ও রিকুইজিশন — কাউন্ট শিটের ছবি তুলুন; অ্যাপ পড়ে প্রতিটি লাইন নিশ্চিত করতে বলে। নিশ্চিত গণনা থেকে খরচ, অসঙ্গতি ও রিকুইজিশন তালিকা আসে।

### 7. SOP templates and assignments

- Points at: `nav-sop-templates`
- **EN:** Templates are the checklists themselves, versioned so history never changes under you. Assignments say which outlet runs which template, when, and for which role.
- **BN:** SOP টেমপ্লেট ও অ্যাসাইনমেন্ট — টেমপ্লেটই চেকলিস্ট, সংস্করণসহ, তাই ইতিহাস বদলায় না। অ্যাসাইনমেন্ট বলে কোন আউটলেটে কোন টেমপ্লেট, কখন, কোন ভূমিকার জন্য চলবে।

### 8. Reference photos: what 'clean' looks like

- Points at: `nav-reference-photos`
- **EN:** Capture one good photo per item at your outlet. The AI reviewer compares every submitted photo against it; without one it can only guess.
- **BN:** রেফারেন্স ছবি: 'পরিষ্কার' দেখতে কেমন — আপনার আউটলেটে প্রতিটি আইটেমের একটি ভালো ছবি তুলে রাখুন। AI রিভিউয়ার প্রতিটি জমা দেওয়া ছবি এর সঙ্গে মেলায়; এটি না থাকলে শুধু অনুমান করে।

### 9. People: roles, PINs and training

- Points at: `nav-people`
- **EN:** Invite managers, set staff PINs for the shared tablet, and see who has finished this walkthrough. Restart someone's training from their card when a person changes.
- **BN:** মানুষ: ভূমিকা, PIN ও প্রশিক্ষণ — ম্যানেজারদের আমন্ত্রণ জানান, শেয়ার্ড ট্যাবলেটের জন্য স্টাফের PIN দিন, আর কে এই পরিচয় শেষ করেছে দেখুন। কেউ বদলালে তাঁর কার্ড থেকে প্রশিক্ষণ আবার চালু করুন।

### 10. Tablets: the shared floor device *(owner and ops manager only)*

- Points at: `nav-tablets`
- **EN:** Each outlet's tablet has its own device account. Register it here once; staff then identify on it with their PIN. Revoke it here if a tablet goes missing.
- **BN:** ট্যাবলেট: ফ্লোরের শেয়ার্ড ডিভাইস — প্রতিটি আউটলেটের ট্যাবলেটের নিজস্ব ডিভাইস অ্যাকাউন্ট আছে। একবার এখানে নিবন্ধন করুন; স্টাফ তারপর PIN দিয়ে চেনায়। ট্যাবলেট হারালে এখান থেকে বাতিল করুন।

### 11. Settings and job runs *(owner and ops manager only)*

- Points at: `nav-settings`
- **EN:** Targets, weights and the scheduled job times live in Settings, with history. Job Runs shows every automatic job that ran overnight and whether it succeeded.
- **BN:** সেটিংস ও জব রান — লক্ষ্য, ওজন ও নির্ধারিত জবের সময় সেটিংসে থাকে, ইতিহাসসহ। Job Runs-এ রাতে চলা প্রতিটি স্বয়ংক্রিয় জব ও তার ফলাফল দেখা যায়।

### 12. Sign out when you leave a shared device

- Points at: `signout`
- **EN:** Your login can approve checklists and change settings. On a tablet or phone that others touch, sign out when you are done.
- **BN:** শেয়ার্ড ডিভাইস ছাড়ার সময় সাইন আউট করুন — আপনার লগইন দিয়ে চেকলিস্ট অনুমোদন ও সেটিংস বদলানো যায়। অন্যরা যে ট্যাবলেট বা ফোন ব্যবহার করে, সেখানে কাজ শেষে সাইন আউট করুন।

### 13. That is the tour

- Centred card, no control highlighted
- **EN:** Your completion is recorded with today's date. You can run this again any time from the bottom of the menu.
- **BN:** পরিচয় শেষ — আপনার সম্পন্ন করা আজকের তারিখসহ নথিভুক্ত হল। মেনুর নিচ থেকে যেকোনো সময় আবার দেখতে পারেন।

## Floor tour (shift lead, staff, on the shared tablet)

### 1. Welcome to the AKIRA tablet

- Centred card, no control highlighted
- **EN:** A one-minute walk through the tablet before your first checklist. Tap Next to move on.
- **BN:** AKIRA ট্যাবলেটে স্বাগতম — প্রথম চেকলিস্টের আগে ট্যাবলেটের এক মিনিটের পরিচয়। এগোতে Next চাপুন।

### 2. This is you

- Points at: `floor-switch`
- **EN:** Your name shows here after you enter your PIN. Everything you do on this tablet is recorded under your name. Never share your PIN.
- **BN:** এটি আপনি — PIN দেওয়ার পর এখানে আপনার নাম দেখায়। এই ট্যাবলেটে আপনি যা করবেন সব আপনার নামে নথিভুক্ত হয়। PIN কাউকে বলবেন না।

### 3. Today's checklists

- Points at: `floor-today`
- **EN:** The list shows what is due for your role today, with its due time. Overdue ones turn red. Finished ones show as waiting for review or approved.
- **BN:** আজকের চেকলিস্ট — আজ আপনার ভূমিকার জন্য যা করার আছে, সময়সহ এখানে দেখায়। দেরি হলে লাল হয়ে যায়। শেষ হলে 'রিভিউর অপেক্ষায়' বা 'অনুমোদিত' দেখায়।

### 4. Open a checklist

- Points at: `floor-run-card`
- **EN:** Tap a card to start. Items come one at a time: read the instruction, do the task, then answer Pass, Fail or Not applicable.
- **BN:** একটি চেকলিস্ট খুলুন — শুরু করতে কার্ডে চাপুন। আইটেম একটি একটি করে আসে: নির্দেশ পড়ুন, কাজটি করুন, তারপর Pass, Fail বা Not applicable বেছে নিন।

### 5. Photo proof

- Centred card, no control highlighted
- **EN:** Some items ask for a photo. Photograph the work, not people. The photo is checked against a reference shot and reviewed by a manager; it also goes to an AI service for an advisory opinion.
- **BN:** ছবির প্রমাণ — কিছু আইটেমে ছবি লাগে। কাজের ছবি তুলুন, মানুষের নয়। ছবিটি রেফারেন্স ছবির সঙ্গে মেলানো হয় ও ম্যানেজার দেখেন; পরামর্শের জন্য একটি AI পরিষেবাতেও যায়।

### 6. If the wifi drops

- Centred card, no control highlighted
- **EN:** Keep going. Your answers and photos are saved on the tablet and sent when the connection returns. A run is never lost half-way.
- **BN:** ওয়াইফাই চলে গেলে — কাজ চালিয়ে যান। উত্তর ও ছবি ট্যাবলেটে জমা থাকে, সংযোগ ফিরলে পাঠানো হয়। অর্ধেক করা রান কখনও হারায় না।

### 7. Review and submit

- Centred card, no control highlighted
- **EN:** At the end you see everything you answered. Submit sends it to a manager for approval. Sent back? Fix the items and submit again.
- **BN:** দেখে নিয়ে জমা দিন — শেষে আপনার সব উত্তর একসঙ্গে দেখায়। Submit চাপলে ম্যানেজারের অনুমোদনে যায়। ফেরত এলে আইটেমগুলো ঠিক করে আবার জমা দিন।

### 8. Hand over when you are done

- Points at: `floor-handover`
- **EN:** Tap 'switch' or 'Hand over' so the next person enters their own PIN. Otherwise their work is recorded under your name.
- **BN:** কাজ শেষে হস্তান্তর করুন — 'switch' বা 'Hand over' চাপুন, যাতে পরের জন নিজের PIN দেয়। নইলে তাঁর কাজ আপনার নামে লেখা হবে।

### 9. Ready

- Centred card, no control highlighted
- **EN:** That is all. Your training is recorded with today's date. Ask your manager if anything is unclear.
- **BN:** প্রস্তুত — এটুকুই। আপনার প্রশিক্ষণ আজকের তারিখসহ নথিভুক্ত হল। কিছু অস্পষ্ট লাগলে ম্যানেজারকে জিজ্ঞাসা করুন।
