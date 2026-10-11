# Employee Attendance & Analytics API

## Overview

This project implements an Employee Attendance & Analytics API using Python, FastAPI, and MongoDB.

It provides endpoints for employee management, attendance punch-in and punch-out, attendance record retrieval and correction, monthly employee analytics, department summaries, late-arrival leaderboards, department trends, and endpoint explanations.

## Technologies

* Python
* FastAPI
* MongoDB
* PyMongo
* Pydantic
* Uvicorn
* python-dotenv

## Prerequisites

* Python installed
* MongoDB running locally or an accessible MongoDB instance
* pip package manager

## Installation

Clone the repository and open the project directory.

Install the dependencies:

```bash
pip install -r requirements.txt
```

## Environment Configuration

Create a local `.env` file in the project root with the required MongoDB configuration:

```env
MONGO_URI=mongodb://localhost:27017/
MONGO_DB=attendance_db
```

Update these values if your MongoDB instance uses a different connection string or database.

Do not commit `.env` files, credentials, or secrets to GitHub.

## Run the Application

From the project root, run:

```bash
uvicorn app.main:app --port 8000
```

## API Documentation

After starting the application, open:

* Swagger UI: http://127.0.0.1:8000/docs
* ReDoc: http://127.0.0.1:8000/redoc

## Main API Areas

* Health checks
* Employee management
* Attendance punch-in and punch-out
* Attendance retrieval and correction
* Monthly employee analytics
* Department summaries and trends
* Late-arrival leaderboard
* Endpoint explanations

## Implementation Notes

The application uses MongoDB for data persistence. Review the source code and `DECISIONS.md` for implementation details and important design choices.

## Review

See `REVIEW.md` for the documented review findings and identified issues.
