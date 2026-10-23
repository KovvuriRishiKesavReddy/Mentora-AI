# MentoraAI evaluation report

- Date: 2026-10-17 12:45
- Notes: OS Complete notes.docx
- Embedder: hashing2048 (OFFLINE HASHING FALLBACK - lexical only; real semantic results are expected to be equal or better)
- Model: (none - retrieval only)
- Mode: retrieval

## Summary

| Check | Result |
|---|---|
| Retrieval hit rate (expected section among sources) | 40/41 (98%) |
| Best source is the expected section (top-1) | 34/41 (83%) |
| Expected keywords present in retrieved context | 98% |
| Follow-up turns retrieved from the right chapter | 11/12 (92%) |
| Out-of-scope questions NOT strongly grounded | 10/10 (100%) |
| Out-of-scope questions with no evidence at all | 10/10 (100%) |

_Answer-level checks were NOT RUN (no model used). Re-run with `--mode answers` and a Groq key._

## Direct questions

| ID | Question | Hit | Top-1 | Keywords in context |
|---|---|---|---|---|
| Q01 | What are the core functions of an operating system? | yes | yes | 100% |
| Q02 | Explain batch operating systems. | yes | yes | 100% |
| Q03 | What is a monolithic kernel? | yes | yes | 100% |
| Q04 | How does a microkernel work? | yes | yes | 100% |
| Q05 | What is the layered structure of an operating system? | yes | yes | 100% |
| Q06 | What is a hybrid kernel? | yes | yes | 100% |
| Q07 | What is an exokernel? | yes | yes | 100% |
| Q08 | What is a virtual machine? | yes | yes | 100% |
| Q09 | What is a process? | **NO** | no | 0% |
| Q10 | What are the states of a process? | yes | yes | 100% |
| Q11 | What does a process control block contain? | yes | yes | 100% |
| Q12 | What happens during a context switch? | yes | no | 100% |
| Q13 | What are the CPU scheduling criteria? | yes | no | 100% |
| Q14 | What is FCFS scheduling? | yes | yes | 100% |
| Q15 | What is the convoy effect? | yes | yes | 100% |
| Q16 | Explain SJF and SRTF scheduling. | yes | no | 100% |
| Q17 | How does Round Robin scheduling work? | yes | yes | 100% |
| Q18 | What is priority based scheduling? | yes | yes | 100% |
| Q19 | What is the difference between MLQ and MLFQ? | yes | yes | 100% |
| Q20 | What is the critical section problem? | yes | yes | 100% |
| Q21 | What is a semaphore? | yes | yes | 100% |
| Q22 | Explain the producer consumer bounded buffer problem. | yes | yes | 100% |
| Q23 | What are the four necessary conditions for deadlock? | yes | no | 100% |
| Q24 | How are deadlocks handled? | yes | yes | 100% |
| Q25 | What is the difference between logical and physical address? | yes | yes | 100% |
| Q26 | What is paging? | yes | no | 100% |
| Q27 | What is demand paging? | yes | yes | 100% |
| Q28 | What happens when a page fault occurs? | yes | yes | 100% |
| Q29 | What are page replacement algorithms? | yes | yes | 100% |
| Q30 | What is a file system? | yes | yes | 100% |
| Q31 | What are the directory structures? | yes | yes | 100% |
| Q32 | What are the file allocation methods? | yes | yes | 100% |
| Q33 | What is the kernel I/O subsystem? | yes | yes | 100% |
| Q34 | What is the role of device drivers? | yes | yes | 100% |
| Q35 | How is I/O performance optimised? Explain buffering. | yes | yes | 100% |
| Q36 | What is the structure of a disk? | yes | yes | 100% |
| Q37 | What are disk scheduling algorithms and what is seek time? | yes | yes | 100% |
| Q38 | What is disk partitioning? | yes | yes | 100% |
| Q39 | What are the RAID levels? | yes | yes | 100% |
| Q40 | Why do we need system calls? | yes | no | 100% |
| Q41 | How does a system call work? | yes | yes | 100% |

## Follow-up sequences

**F1 - Scheduling follow-ups**

| Message | Right chapter | Understood as |
|---|---|---|
| Explain CPU scheduling | yes (4. CPU Scheduling > Scheduling Algorithms) | - |
| What are its advantages? | yes (4. CPU Scheduling > Scheduling Algorithms) | What are its advantages? (regarding: CPU scheduling) |
| Give an example | yes (4. CPU Scheduling > Scheduling Algorithms) | Give an example (regarding: CPU scheduling) |
| I don't understand it. | yes (4. CPU Scheduling > Scheduling Algorithms) | Explain CPU scheduling in simple words, from the basics. |

**F2 - Deadlock follow-ups**

| Message | Right chapter | Understood as |
|---|---|---|
| Explain deadlocks | yes (6. Deadlocks > Deadlock Handling Strategies) | - |
| How can it be prevented? | **NO** ((none)) | How can it be prevented? (regarding: deadlocks) |
| What are its necessary conditions? | yes (6. Deadlocks > Deadlock Handling Strategies) | What are its necessary conditions? (regarding: deadlocks) |

**F3 - Topic change**

| Message | Right chapter | Understood as |
|---|---|---|
| What is paging? | yes (7. Memory Management > Virtual Memory and Demand Paging) | - |
| What are its advantages? | yes (7. Memory Management > Virtual Memory and Demand Paging) | What are its advantages? (regarding: paging) |
| Now explain the RAID levels | yes (10. Disk Management > RAID Levels (Redundant Array of Independent Disks)) | - |

**F4 - Confusion about memory**

| Message | Right chapter | Understood as |
|---|---|---|
| Explain virtual memory | yes (7. Memory Management > Virtual Memory and Demand Paging) | - |
| I don't understand it. | yes (7. Memory Management > Virtual Memory and Demand Paging) | Explain virtual memory in simple words, from the basics. |

## Out-of-scope questions

| ID | Question | Grounding | OK |
|---|---|---|---|
| O01 | Explain quantum chromodynamics and gluon confinement | none | yes |
| O02 | How do I bake sourdough bread at home? | none | yes |
| O03 | Who won the 2018 football world cup? | none | yes |
| O04 | Explain blockchain consensus mechanisms | none | yes |
| O05 | How do I configure a Kubernetes ingress controller? | none | yes |
| O06 | Describe the light reactions of photosynthesis | none | yes |
| O07 | What are Python decorators? | none | yes |
| O08 | What are the tax rules for freelancers? | none | yes |
| O09 | Summarise the history of the Roman empire | none | yes |
| O10 | How does GraphQL schema stitching work? | none | yes |

## Failures

Retrieval misses: Q09
