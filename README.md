# ConsultBae – Duplicate Detection & Alert Automation

## Overview

This project implements an automation workflow for detecting duplicate
candidate/person records in a ConsultBae-style system.

The workflow receives an email through a webhook, validates and normalizes
the email address, checks the backend for an existing person record, and
generates and sends a duplicate alert when a matching record is found.

## Problem Statement

Candidate/person data can contain duplicate records when the same person
is submitted multiple times using the same or differently formatted email.

The objective of this automation is to:

- Accept incoming candidate data through a webhook
- Validate the required email field
- Normalize the email before lookup
- Query the backend for an existing person
- Detect whether a duplicate exists
- Generate a structured duplicate alert
- Send the alert to the alert endpoint

## Architecture

```text
Incoming Request
       |
       v
   Webhook
       |
       v
 Validate Email
       |
       v
 Normalize Email
       |
       v
 HTTP Request
(Person Lookup API)
       |
       v
  Duplicate Found?
      / \
    Yes  No
     |
     v
Duplicate Alert
     |
     v
Send Duplicate Alert
