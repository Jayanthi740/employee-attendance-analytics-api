# Implementation Decisions

## 1. Technology Stack

The application uses Python and FastAPI to implement the REST API. MongoDB is used for persistence, with PyMongo for database operations.

## 2. Employee Identification

The application uses `emp_code` as the employee business identifier. MongoDB's internal `_id` is not intended to replace the employee business identifier in API operations.

## 3. Configuration

MongoDB connection settings are supplied through environment variables, including `MONGO_URI` and `MONGO_DB`. Credentials and local environment configuration are excluded from the public repository.

## 4. Date and Time Handling

The implementation follows the assignment's requirements for Indian Standard Time (IST), epoch-millisecond API timestamps, BSON datetime storage, date strings, and shift times.

## 5. Attendance Processing

Attendance operations are implemented through dedicated punch-in, punch-out, retrieval, and correction endpoints. The implementation must follow the specified rules for overnight shifts, late-arrival grace periods, rounding, and attendance statuses.

## 6. Analytics

The application provides monthly employee analytics, department summaries, late-arrival rankings, and department attendance trends.

## 7. Validation and Error Handling

Request validation and error handling are implemented using FastAPI, Pydantic, and the application's database-operation logic.

## 8. Data Integrity

The implementation should enforce employee identification rules, protect derived fields from direct modification, and preserve the required history of manual attendance corrections.

## 9. Pagination

Paginated endpoints should follow the assignment's maximum page-size requirement of 100.

## 10. Verification

The implementation and its behavior should be verified against the supplied API contract and business rules before submission.
